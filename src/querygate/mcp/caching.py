"""Force SEP-2549 `cache_scope: "private"` onto every principal-varying MCP result.

TODO.md item 129. The MCP `2026-07-28` revision adds caching metadata
(SEP-2549) to `tools/list`, `prompts/list`, `resources/list`, and
`resources/read`: a `cache_scope` of `"public"` or `"private"`, modelled on
HTTP `Cache-Control`, where `"public"` permits a shared intermediary (a
fronting gateway/proxy) to cache the response and reuse it across callers.

QueryGate's MCP surface is per-principal by construction (`mcp/server.py`'s
`_install_scoped_tool_listing` filters `tools/list` by the caller's scopes),
and the spec explicitly blesses this ("the set MAY vary by the authorization
presented on the request"). But a principal-varying result ever marked
`"public"` could be served across principals by a caching gateway, eroding
the deny-by-default posture without a single line of policy code being
wrong. This is defense-in-depth, not an authorization bypass: the real
boundary stays each tool's own call-time scope check.

**The installed SDK (`mcp` v2) already defaults `cache_scope="private"`
on every relevant result type** — `ListToolsResult`, `ListPromptsResult`,
`ListResourcesResult`, and `ReadResourceResult` all inherit
`mcp_types.CacheableResult`, whose module docstring states the SDK defaults
`cache_scope="private"` precisely "so a handler that doesn't set them still
produces a valid 2026-07-28 result without accidentally enabling shared
caching." `CallToolResult`/`GetPromptResult` (the two explicitly out-of-scope
surfaces — a tool's/prompt's per-caller *call-time result*, not a listed
surface under the spec) are plain `Result` subclasses with no such field.

So the SDK default alone already satisfies this item's invariant today. This
module exists anyway, per CLAUDE.md's "the failure mode is a default, not a
decision" doctrine (the same reasoning `test_credential_redaction.py` uses):
relying on an upstream default, however correct today, is not the same as
QueryGate owning the guarantee. `install_private_cache_scope` wraps the four
relevant low-level request handlers to force `cache_scope="private"`
unconditionally on their result, independent of the SDK's own default —
so a future SDK version that changes its default, or a future QueryGate
handler that explicitly (even if accidentally) sets `cache_scope="public"`,
cannot silently regress this. QueryGate registers zero resources/prompts
today; the wrapper applies unconditionally to the *request type*, not to
individual registrations, so anything registered in the future inherits the
guarantee with no per-registration opt-in required.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.server.lowlevel.server import HandlerEntry

PRIVATE_CACHE_SCOPE = "private"

# The four low-level JSON-RPC methods SEP-2549 attaches caching metadata to
# (`mcp.server.lowlevel.server.Server._request_handlers` is keyed by method
# string in the v2 SDK, not by request-type class as in v1). Deliberately
# excludes:
#  - `tools/call` / `prompts/get`: a tool's or prompt's per-caller *call-time
#    result* is not a listed/cacheable surface under the spec (their result
#    types, `CallToolResult`/`GetPromptResult`, don't even declare the
#    field) — marking it would be a mis-aim. Guarded by
#    `test_tools_call_result_is_not_annotated_with_cache_scope` and
#    `test_get_prompt_result_is_not_annotated_with_cache_scope` in
#    `tests/unit/test_mcp_cache_scope.py`.
#  - `resources/templates/list`: no spec artifact available in this repo
#    names resource templates as a SEP-2549 caching-metadata surface, and
#    QueryGate registers zero resource templates today (zero present impact
#    either way). Note the installed SDK's `ListResourceTemplatesResult` DOES
#    inherit `CacheableResult` (defaults to private the same as the other
#    four), so this exclusion costs nothing even if the spec does cover it —
#    a reasoned exclusion recorded here rather than a guess.
_CACHE_SCOPED_METHODS: tuple[str, ...] = (
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/read",
)

RequestHandler = Callable[[Any, Any], Awaitable[Any]]

# Attribute name used to mark a handler as already forcing cache_scope, so a
# second wrap (repeated `install_private_cache_scope` calls, or a handler
# re-registered without changing) is a detected no-op instead of nesting
# another closure layer around it every time.
_FORCED_MARKER = "_qg_forced_private_cache_scope"


def with_forced_private_cache_scope(inner_handler: RequestHandler) -> RequestHandler:
    """Wrap a low-level MCP request handler to force `cache_scope="private"`.

    `inner_handler` is the `.handler` of one of the low-level
    `Server._request_handlers` entries for `tools/list`, `prompts/list`,
    `resources/list`, or `resources/read` — called as `(ctx, params)` and
    returning the result model directly (`ListToolsResult`/
    `ListPromptsResult`/`ListResourcesResult`/`ReadResourceResult`, all
    `mcp_types.CacheableResult` subclasses with a real `cache_scope` field,
    not `extra="allow"` field-forcing). Setting `.cache_scope` post-return is
    a normal, supported pydantic assignment on a declared field, verified
    against the emitted JSON-RPC payload in `tests/unit/test_mcp_cache_scope.py`.

    This never inspects *what* the result contains — it applies
    unconditionally to any result of these four request types, so a future
    resource or prompt registration is covered automatically rather than
    needing its own cache-scope opt-in.

    Idempotent: if `inner_handler` is already marked (a prior call already
    wrapped it), returns it unchanged rather than nesting a second closure
    around it. Without this, repeated installation (e.g.
    `create_mcp_server()` called more than once — several existing unit
    tests do exactly that against the shared module-level `mcp_server`
    singleton) would grow an unbounded wrapper chain, one layer per call —
    functionally invisible today because the only side effect (setting
    `cache_scope` to the same value repeatedly) is itself idempotent, but a
    latent trap the moment that side effect ever becomes anything
    non-idempotent (an audit event, a metrics counter, a log line).
    """
    if getattr(inner_handler, _FORCED_MARKER, False):
        return inner_handler

    async def wrapped(ctx: Any, params: Any) -> Any:
        result = await inner_handler(ctx, params)
        result.cache_scope = PRIVATE_CACHE_SCOPE
        return result

    setattr(wrapped, _FORCED_MARKER, True)
    return wrapped


class _PrivateCacheScopeHandlers(dict):
    """A `Server._request_handlers` dict that auto-wraps any assignment to
    one of the four SEP-2549 cache-scoped methods.

    The naive fix — wrap whichever handler happens to be installed *right
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

    def __setitem__(self, key: str, value: HandlerEntry) -> None:
        if key in _CACHE_SCOPED_METHODS:
            value = HandlerEntry(value.params_type, with_forced_private_cache_scope(value.handler))
        super().__setitem__(key, value)


def install_private_cache_scope(mcp_server: Any) -> None:
    """Install a self-enforcing handler dict on `mcp_server`'s low-level Server.

    Call this any time relative to other handler installers (e.g.
    `_install_scoped_tool_listing`) — before, after, or interleaved with a
    future re-registration — every assignment to one of the four cache-scoped
    methods routes through `_PrivateCacheScopeHandlers.__setitem__` and gets
    wrapped, so the guarantee does not depend on getting the call order right
    at every future call site.

    Idempotent: if `mcp_server`'s low-level `Server._request_handlers` is
    already a `_PrivateCacheScopeHandlers` (a previous call already installed
    it), this is a no-op.

    Raises `RuntimeError` rather than silently skipping if any of the four
    cache-scoped methods has no handler registered at all — on a real
    `MCPServer` instance this can't happen (all four are registered
    unconditionally at construction time), so if it ever does, that's a
    signal something is badly wrong with the server object being passed in,
    not something to paper over with a silent `continue`.
    """
    server = mcp_server._lowlevel_server
    handlers = server._request_handlers
    if isinstance(handlers, _PrivateCacheScopeHandlers):
        return

    missing = [m for m in _CACHE_SCOPED_METHODS if m not in handlers]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            "install_private_cache_scope: no handler registered for "
            f"{names} — a real MCPServer instance registers all four "
            "SEP-2549 surfaces at construction time, so refusing to install "
            "a cache-scope guarantee that can't cover all of them."
        )

    enforcing = _PrivateCacheScopeHandlers()
    for key, value in handlers.items():
        enforcing[key] = value  # routes through __setitem__, wraps the 4 relevant keys
    server._request_handlers = enforcing


def assert_private_cache_scope_installed(mcp_server: Any) -> None:
    """Fail loudly, before the ASGI app is mounted, if the SEP-2549
    cache-scope guarantee somehow isn't installed on `mcp_server`.

    Belt-and-suspenders on top of `_PrivateCacheScopeHandlers`'s structural
    guarantee: called from `mcp/server.py`'s `setup_mcp` right before
    `streamable_http_app()`, so a future refactor that skips
    `install_private_cache_scope` entirely, or swaps `_request_handlers` out
    for a plain dict, can never silently reach a served app.
    """
    handlers = mcp_server._lowlevel_server._request_handlers
    if not isinstance(handlers, _PrivateCacheScopeHandlers):
        raise RuntimeError(
            "MCP server is about to be served without the SEP-2549 "
            "cache_scope=private guarantee installed (TODO.md item 129) — "
            "call querygate.mcp.caching.install_private_cache_scope() first."
        )
    unmarked = []
    for m in _CACHE_SCOPED_METHODS:
        entry = handlers.get(m)
        handler = entry.handler if entry is not None else None
        if not getattr(handler, _FORCED_MARKER, False):
            unmarked.append(m)
    if unmarked:
        names = ", ".join(unmarked)
        raise RuntimeError(
            "MCP server is about to be served, but the following SEP-2549 "
            f"request handlers are not forcing cache_scope=private: {names} "
            "(TODO.md item 129)."
        )
