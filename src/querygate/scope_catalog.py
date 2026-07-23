"""Render the operator-facing scope-catalog reference from `core/scopes.py`.

The output (`docs/SCOPE_CATALOG.md`, produced by `querygate-scope-catalog`) is
generated, never hand-maintained, so it cannot drift from the enforced scope
constants (TODO.md item 95). A test regenerates it and asserts the committed
copy matches, the same drift guard the SBOM and other generated artifacts use.
"""

from __future__ import annotations

from querygate.core.scopes import ROLE_BUNDLES, SCOPE_CATALOG

_HEADER = """<!-- GENERATED FILE — do not edit by hand.
     Regenerate with: make scope-catalog  (or: poetry run querygate-scope-catalog)
     Source of truth: src/querygate/core/scopes.py -->

# QueryGate authorization scope catalog

QueryGate does not own identities — you bring your IdP (Okta, Entra, Auth0,
Keycloak, …) and QueryGate verifies its JWTs (item 10) as an OAuth resource
server (item 90). For that to work, your authorization server needs QueryGate's
scope vocabulary. This page is that vocabulary, plus recommended role bundles to
paste into your IdP. It is generated from `core/scopes.py`, so it always matches
what the running server actually enforces.

Two things stay out of the IdP by design:

- **Data-access grants** (which tables/columns a principal may read) live in
  `policy.yaml`, keyed by `sub`/claim — never as scopes. An "Analyst" therefore
  carries *no* scope and is governed entirely by data policy.
- The same vocabulary is machine-discoverable at the RFC 9728 endpoint
  `/.well-known/oauth-protected-resource/mcp` (`scopes_supported`) when the MCP
  OAuth resource server is enabled, so an IdP can import it without any typing.

## Scopes

Every scope QueryGate understands, grouped by area. Each gates exactly one class
of privileged action; unlisted actions (running a structured query) need no
scope, only a valid identity.
"""


def render_scope_catalog_markdown() -> str:
    """Return the full Markdown reference. Deterministic, so a drift test can
    compare it byte-for-byte against the committed `docs/SCOPE_CATALOG.md`."""
    lines: list[str] = [_HEADER.rstrip(), ""]

    # Scopes grouped by category, preserving SCOPE_CATALOG order within a group.
    seen_categories: list[str] = []
    by_category: dict[str, list[tuple[str, str]]] = {}
    for info in SCOPE_CATALOG:
        if info.category not in by_category:
            by_category[info.category] = []
            seen_categories.append(info.category)
        by_category[info.category].append((info.scope, info.gates))

    for category in seen_categories:
        lines.append(f"### {category}")
        lines.append("")
        lines.append("| Scope | Gates |")
        lines.append("|---|---|")
        for scope, gates in by_category[category]:
            lines.append(f"| `{scope}` | {gates} |")
        lines.append("")

    lines.append("## Recommended role bundles")
    lines.append("")
    lines.append(
        "Advisory groupings — QueryGate enforces individual scopes, not roles. "
        "Register each as a role in your IdP and grant it the listed scopes; "
        "provisioning a user is then a role assignment."
    )
    lines.append("")
    for bundle in ROLE_BUNDLES:
        lines.append(f"### {bundle.name}")
        lines.append("")
        lines.append(bundle.purpose)
        lines.append("")
        if bundle.scopes:
            for scope in bundle.scopes:
                lines.append(f"- `{scope}`")
        else:
            lines.append("- _(no scopes — governed by data policy only)_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
