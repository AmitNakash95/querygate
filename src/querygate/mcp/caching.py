"""Force SEP-2549 `cacheScope: "private"` onto every principal-varying MCP result.

TODO.md item 129. The MCP `2026-07-28` revision adds caching metadata
(SEP-2549) to `tools/list`, `prompts/list`, `resources/list`, and
`resources/read`: a `cacheScope` of `"public"` or `"private"`, modelled on
HTTP `Cache-Control`, where `"public"` permits a shared intermediary (a
fronting gateway/proxy) to cache the response and reuse it across callers.

QueryGate's MCP surface is per-principal by construction (`mcp/server.py`'s
`_install_scoped_tool_listing` filters `tools/list` by the caller's scopes),
and the spec explicitly blesses this ("the set MAY vary by the authorization
presented on the request"). But a principal-varying result ever marked
`"public"` — or left unmarked, which some intermediaries treat as
cacheable-by-default — could be served across principals by a caching
gateway, eroding the deny-by-default posture without a single line of policy
code being wrong. This is defense-in-depth, not an authorization bypass: the
real boundary stays each tool's own call-time scope check.

QueryGate registers zero resources and zero prompts today, so this module
does not special-case "there's nothing to protect yet" — it wraps the
low-level request handler for all four cacheable request types
unconditionally (`install_private_cache_scope`), so a resource or prompt
registered in the future inherits the same enforced `cacheScope: "private"`
with no per-registration opt-in required.

Interop note: the installed `mcp` SDK (1.28.1) reports
`LATEST_PROTOCOL_VERSION = "2025-11-25"` and defines no `cacheScope` field at
all — SEP-2549 is a `2026-07-28` addition (TODO.md item 128, not yet shipped
on this branch). This module forces the field on anyway by relying on every
relevant `mcp.types` result class declaring `model_config =
ConfigDict(extra="allow")`; it costs nothing to a client speaking the older
protocol (an unrecognized field is simply ignored) and needs no rework once
128 lands with native SDK support for the field.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp import types as mcp_types

PRIVATE_CACHE_SCOPE = "private"

# The four request types SEP-2549 attaches caching metadata to. Deliberately
# excludes:
#  - `CallToolRequest` / `GetPromptRequest`: a tool's or prompt's per-caller
#    *call-time result* is not a listed/cacheable surface under the spec —
#    marking it would be a mis-aim (TODO.md item 129's write-up). Guarded by
#    `test_tools_call_result_is_not_annotated_with_cache_scope` and
#    `test_get_prompt_result_is_not_annotated_with_cache_scope` in
#    `tests/unit/test_mcp_cache_scope.py`.
#  - `ListResourceTemplatesRequest`: no spec artifact available in this repo
#    names resource templates as a SEP-2549 caching-metadata surface, the
#    installed SDK (1.28.1, protocol `2025-11-25`) predates SEP-2549 entirely
#    so there is nothing authoritative to check it against, and QueryGate
#    registers zero resource templates today (zero present impact either
#    way). A reasoned exclusion recorded here rather than a guess — revisit
#    once item 128 lands with a real `2026-07-28` SDK and spec text to check.
_CACHE_SCOPED_REQUEST_TYPES: tuple[type, ...] = (
    mcp_types.ListToolsRequest,
    mcp_types.ListPromptsRequest,
    mcp_types.ListResourcesRequest,
    mcp_types.ReadResourceRequest,
)

InnerHandler = Callable[..., Awaitable[mcp_types.ServerResult]]

# Attribute name used to mark a handler as already forcing cacheScope, so a
# second wrap (repeated `install_private_cache_scope` calls, or a handler
# re-registered without changing) is a detected no-op instead of nesting
# another closure layer around it every time.
_FORCED_MARKER = "_qg_forced_private_cache_scope"


def with_forced_private_cache_scope(inner_handler: InnerHandler) -> InnerHandler:
    """Wrap a low-level MCP request handler to force `cacheScope: "private"`.

    `inner_handler` is one of the low-level `Server.request_handlers` entries
    for `tools/list`, `prompts/list`, `resources/list`, or `resources/read` —
    it always returns a `types.ServerResult` whose `.root` is the actual
    result object (`ListToolsResult`/`ListPromptsResult`/`ListResourcesResult`/
    `ReadResourceResult`). Every one of those result classes declares
    `model_config = ConfigDict(extra="allow")`, so setting `.cacheScope`
    post-construction is a normal, supported pydantic assignment that survives
    `model_dump(by_alias=True)` serialization (verified against the emitted
    JSON-RPC payload in `tests/unit/test_mcp_cache_scope.py`).

    This never inspects *what* the result contains — it applies unconditionally
    to any result of these four request types, so a future resource or prompt
    registration is covered automatically rather than needing its own
    cache-scope opt-in.

    Idempotent: if `inner_handler` is already marked (a prior call already
    wrapped it), returns it unchanged rather than nesting a second closure
    around it. Without this, repeated installation (e.g.
    `create_mcp_server()` called more than once — several existing unit
    tests do exactly that against the shared module-level `mcp_server`
    singleton) would grow an unbounded wrapper chain around
    `prompts/list`/`resources/list`/`resources/read`'s handlers, one layer
    per call — functionally invisible today because the only side effect
    (setting `cacheScope` to the same value repeatedly) is itself idempotent,
    but a latent trap the moment that side effect ever becomes anything
    non-idempotent (an audit event, a metrics counter, a log line).
    """
    if getattr(inner_handler, _FORCED_MARKER, False):
        return inner_handler

    async def wrapped(req=None):
        server_result = await inner_handler(req)
        server_result.root.cacheScope = PRIVATE_CACHE_SCOPE
        return server_result

    setattr(wrapped, _FORCED_MARKER, True)
    return wrapped


class _PrivateCacheScopeHandlers(dict):
    """A `Server.request_handlers` dict that auto-wraps any assignment to one
    of the four SEP-2549 cache-scoped request types.

    The naive fix — wrap whatever handler happens to be installed *right
    now* — is installation-order-dependent: if anything re-registers one of
    these four handlers afterward (e.g. `_install_scoped_tool_listing`
    called a second time by some future config-reload path, or simply called
    *after* `install_private_cache_scope` instead of before), the fresh,
    unwrapped handler silently replaces the wrapped one and the cache-scope
    guarantee regresses with no signal. Overriding `__setitem__` instead
    means ANY future assignment to one of the four keys is wrapped at write
    time, for the lifetime of this dict — independent of call order and
    independent of how many times a handler gets re-registered. See
    `test_cache_scope_survives_reinstallation_in_either_order` in
    `tests/unit/test_mcp_cache_scope.py`.
    """

    def __setitem__(self, key: type, value: InnerHandler) -> None:
        if key in _CACHE_SCOPED_REQUEST_TYPES:
            value = with_forced_private_cache_scope(value)
        super().__setitem__(key, value)


def install_private_cache_scope(mcp_server: Any) -> None:
    """Install a self-enforcing handler dict on `mcp_server`'s low-level Server.

    Call this any time relative to other handler installers (e.g.
    `_install_scoped_tool_listing`) — before, after, or interleaved with a
    future re-registration — every assignment to one of the four cache-scoped
    request types routes through `_PrivateCacheScopeHandlers.__setitem__` and
    gets wrapped, so the guarantee does not depend on getting the call order
    right at every future call site.

    Idempotent: if `mcp_server`'s low-level `Server.request_handlers` is
    already a `_PrivateCacheScopeHandlers` (a previous call already installed
    it), this is a no-op.

    Raises `RuntimeError` rather than silently skipping if any of the four
    cache-scoped request types has no handler registered at all — on a real
    `FastMCP` instance this can't happen (all four are registered
    unconditionally at construction time), so if it ever does, that's a
    signal something is badly wrong with the server object being passed in,
    not something to paper over with a silent `continue`.
    """
    server = mcp_server._mcp_server
    handlers = server.request_handlers
    if isinstance(handlers, _PrivateCacheScopeHandlers):
        return

    missing = [t for t in _CACHE_SCOPED_REQUEST_TYPES if t not in handlers]
    if missing:
        names = ", ".join(t.__name__ for t in missing)
        raise RuntimeError(
            "install_private_cache_scope: no handler registered for "
            f"{names} — a real FastMCP instance registers all four SEP-2549 "
            "surfaces at construction time, so refusing to install a "
            "cache-scope guarantee that can't cover all of them."
        )

    enforcing = _PrivateCacheScopeHandlers()
    for key, value in handlers.items():
        enforcing[key] = value  # routes through __setitem__, wraps the 4 relevant keys
    server.request_handlers = enforcing


def assert_private_cache_scope_installed(mcp_server: Any) -> None:
    """Fail loudly, before the ASGI app is mounted, if the SEP-2549
    cache-scope guarantee somehow isn't installed on `mcp_server`.

    Belt-and-suspenders on top of `_PrivateCacheScopeHandlers`'s structural
    guarantee: called from `mcp/server.py`'s `setup_mcp` right before
    `streamable_http_app()`, so a future refactor that skips
    `install_private_cache_scope` entirely, or swaps `request_handlers` out
    for a plain dict, can never silently reach a served app.
    """
    handlers = mcp_server._mcp_server.request_handlers
    if not isinstance(handlers, _PrivateCacheScopeHandlers):
        raise RuntimeError(
            "MCP server is about to be served without the SEP-2549 "
            "cacheScope=private guarantee installed (TODO.md item 129) — "
            "call querygate.mcp.caching.install_private_cache_scope() first."
        )
    unmarked = [
        t.__name__
        for t in _CACHE_SCOPED_REQUEST_TYPES
        if not getattr(handlers.get(t), _FORCED_MARKER, False)
    ]
    if unmarked:
        names = ", ".join(unmarked)
        raise RuntimeError(
            "MCP server is about to be served, but the following SEP-2549 "
            f"request handlers are not forcing cacheScope=private: {names} "
            "(TODO.md item 129)."
        )
