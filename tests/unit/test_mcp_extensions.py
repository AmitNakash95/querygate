"""Conformance tests for the `io.github.agitmit/structured-query-ast` MCP
extension (TODO.md item 131).

Four things are proven here, matching the item's own acceptance bar:

1. **No drift** — the committed schema file is byte-for-byte what a fresh
   `generate_structured_query_ast_schema()` call produces right now, the same
   fresh-vs-committed drift-guard idea `test_credential_redaction.py` uses for
   the credential invariant and `test_client_builder.py` uses for the
   TypeScript client builder — though unlike those two, this one generates in
   a subprocess rather than in-process; see
   `_generate_schema_in_fresh_interpreter`'s own docstring for why. If
   `query_ast/models.py`/`write_ast/models.py` change without regenerating the
   file, this test fails.
2. **Really declared** — a real `create_mcp_server()` instance's emitted
   capabilities (not just source-reading `mcp/extensions.py`) advertise the
   extension under `ServerCapabilities.extensions`.
3. **The hard boundary holds against a new method** — no JSON-RPC method
   matching the extension's namespace is registered anywhere on the server,
   structurally, not just by the module docstring's say-so. This is the test
   that would fail if a future change accidentally turned the contract into a
   callable method.
4. **The hard boundary holds against a `tools/call` interceptor** —
   `Extension.intercept_tool_call` (the SDK's fourth contribution point,
   which can wrap or short-circuit `tools/call` itself) is never overridden,
   checked by identity against the base class on both the class itself and
   every extension actually installed on a real server.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mcp.server.extension import Extension

from querygate.mcp.extensions import (
    STRUCTURED_QUERY_AST_EXTENSION_ID,
    STRUCTURED_QUERY_AST_EXTENSION_VERSION,
    STRUCTURED_QUERY_AST_SCHEMA_PATH,
    STRUCTURED_QUERY_AST_SPEC_PATH,
    StructuredQueryAstExtension,
    generate_structured_query_ast_schema,
)
from querygate.mcp.server import create_mcp_server

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHED_SCHEMA_FILE = _REPO_ROOT / STRUCTURED_QUERY_AST_SCHEMA_PATH
_SPEC_DOC_FILE = _REPO_ROOT / STRUCTURED_QUERY_AST_SPEC_PATH


def _generate_schema_in_fresh_interpreter() -> dict:
    """Generate the schema in a brand-new interpreter, not this test process.

    `scripts/generate_mcp_extension_schema.py` is always run standalone
    (`make mcp-extension-schema`), so the drift guard below reproduces that
    exact condition rather than calling `generate_structured_query_ast_schema()`
    in-process. A shared pytest process ends up constructing many
    `TypeAdapter`s and schemas over models that share `StructuredQuery` as a
    `$ref` target (`CteSpec.query`, `SetOpSpec.arms`, `Predicate.value_subquery`,
    ...); pydantic's JSON-schema generator was observed to occasionally
    misattach a `$ref`'s sibling `description` between two such occurrences
    depending on unrelated schema-generation activity earlier in the same
    process — a real determinism gap in a big, long-lived process that a
    subprocess sidesteps by construction, the same way the committed file is
    actually produced.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; "
            "from querygate.mcp.extensions import generate_structured_query_ast_schema; "
            "print(json.dumps(generate_structured_query_ast_schema()))",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_generated_schema_matches_published_file():
    """The committed schema file must be exactly what the live AST models
    produce right now — a stale file means an AST change shipped without
    regenerating it (`make mcp-extension-schema`)."""
    generated = _generate_schema_in_fresh_interpreter()
    published = json.loads(_PUBLISHED_SCHEMA_FILE.read_text(encoding="utf-8"))
    assert generated == published, (
        "docs/mcp_extensions/structured_query_ast.schema.json is stale — "
        "regenerate it with `make mcp-extension-schema` (or "
        "`poetry run python scripts/generate_mcp_extension_schema.py`) after "
        "the AST change that caused this drift."
    )


def test_published_schema_file_is_canonically_formatted():
    """The committed file must be exactly what the generator script writes
    (2-space indent, sorted keys, trailing newline) — not just semantically
    equal — so a hand-edit shows up as a diff a reviewer would notice."""
    generated = _generate_schema_in_fresh_interpreter()
    expected_bytes = json.dumps(generated, indent=2, sort_keys=True) + "\n"
    assert _PUBLISHED_SCHEMA_FILE.read_text(encoding="utf-8") == expected_bytes


def test_schema_covers_read_and_write_ast():
    schema = generate_structured_query_ast_schema()
    assert schema["extension"] == STRUCTURED_QUERY_AST_EXTENSION_ID
    assert schema["version"] == STRUCTURED_QUERY_AST_EXTENSION_VERSION
    # StructuredQuery's own top-level schema is a $ref into $defs (self-
    # referencing WhereGroup nesting) — assert the definition is actually
    # present under both the read and write halves, not just a bare pointer.
    assert "StructuredQuery" in schema["read"]["$defs"]
    write_defs = schema["write"]["$defs"]
    for write_shape in ("InsertStatement", "UpdateStatement", "DeleteStatement", "UpsertStatement"):
        assert write_shape in write_defs


def test_spec_doc_exists_and_states_the_non_goal():
    """The spec document must exist and explicitly disclaim a JSON-RPC method
    — the hard boundary CLAUDE.md and TODO.md item 131 both require to be
    stated in the spec itself, not just enforced in code."""
    assert _SPEC_DOC_FILE.is_file()
    text = _SPEC_DOC_FILE.read_text(encoding="utf-8")
    assert "Non-goals" in text
    assert (
        "never introduces a JSON-RPC method" in text or "never introduce a JSON-RPC method" in text
    )


def test_extension_declared_in_real_server_capabilities():
    """Asserted against a real `create_mcp_server()` instance's emitted
    capabilities, not just by reading `mcp/extensions.py`'s source."""
    server = create_mcp_server()
    capabilities = server._lowlevel_server.get_capabilities()
    assert capabilities.extensions is not None
    assert STRUCTURED_QUERY_AST_EXTENSION_ID in capabilities.extensions
    settings = capabilities.extensions[STRUCTURED_QUERY_AST_EXTENSION_ID]
    assert settings["version"] == STRUCTURED_QUERY_AST_EXTENSION_VERSION
    assert settings["kind"] == "contract"
    assert settings["spec"] == STRUCTURED_QUERY_AST_SPEC_PATH
    assert settings["schema"] == STRUCTURED_QUERY_AST_SCHEMA_PATH

    # Also present on the raw extensions map `_apply_extension` populates
    # (what `create_initialization_options()` defaults to), not only on a
    # manually-passed capabilities call.
    assert server._lowlevel_server.extensions.get(STRUCTURED_QUERY_AST_EXTENSION_ID) == settings


def test_extension_identifier_is_registered_exactly_once():
    server = create_mcp_server()
    identifiers = [ext.identifier for ext in server._extensions]  # type: ignore[attr-defined]
    assert identifiers.count(STRUCTURED_QUERY_AST_EXTENSION_ID) == 1


def test_extension_contributes_no_tools_resources_or_methods():
    """The extension base class defaults to contributing nothing; this
    asserts `StructuredQueryAstExtension` never overrides those defaults —
    a purely metadata-only extension, by construction."""
    extension = StructuredQueryAstExtension()
    assert extension.tools() == ()
    assert extension.resources() == ()
    assert extension.methods() == ()


def test_extension_does_not_intercept_tool_calls():
    """`Extension` has a fourth contribution point beyond tools/resources/
    methods: `intercept_tool_call`, which can wrap or short-circuit
    `tools/call` itself — the one contribution kind that actually touches
    query execution, since an interceptor can answer a tool call without
    ever reaching the real handler (and its policy/schema/compile/audit
    pipeline). Checked by identity against the unbound base-class method
    (not by calling it) so this fails the moment anyone overrides it, not
    only if the override happens to behave badly."""
    assert StructuredQueryAstExtension.intercept_tool_call is Extension.intercept_tool_call

    # Also checked against every extension actually installed on a real
    # server, not just this one class in isolation — this is what
    # `MCPServer._install_extension_interceptor` inspects to decide whether
    # to replace the bare `tools/call` handler with a wrapped one at all.
    server = create_mcp_server()
    assert all(
        type(ext).intercept_tool_call is Extension.intercept_tool_call
        for ext in server._extensions  # type: ignore[attr-defined]
    )


def test_no_jsonrpc_method_registered_under_the_extension_namespace():
    """Structural guard for the hard boundary: no request method anywhere on
    a real server matches or is scoped under the extension's identifier.
    This is the test that would fail if a future change accidentally added a
    namespaced method that accepts or executes a query."""
    server = create_mcp_server()
    registered_methods = set(server._lowlevel_server._request_handlers.keys())

    # No exact match, no method path scoped under the extension's identifier
    # (e.g. "io.github.agitmit/structured-query-ast/execute"), and no bare
    # method starting with the extension's short name — covers every
    # plausible shape a JSON-RPC method smuggling query execution in under
    # this namespace could take.
    assert STRUCTURED_QUERY_AST_EXTENSION_ID not in registered_methods
    for method in registered_methods:
        assert not method.startswith(f"{STRUCTURED_QUERY_AST_EXTENSION_ID}/"), (
            f"found a JSON-RPC method {method!r} registered under the "
            "structured-query-ast extension's namespace — this extension "
            "must declare a contract only, never a callable method "
            "(see docs/mcp_extensions/structured_query_ast.md's Non-goals)"
        )
        assert not method.startswith("structured-query-ast/"), (
            f"found a JSON-RPC method {method!r} that looks like it "
            "smuggles query execution in under the structured-query-ast "
            "extension's short name"
        )
