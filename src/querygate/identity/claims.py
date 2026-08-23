"""Reading values out of an IdP's claim set.

Every identity provider answers "which groups/roles is this person in?" with
its own claim name *and* its own shape: Entra ID and Okta put a flat array at a
top-level claim, Keycloak nests realm roles at `realm_access.roles`, and some
providers emit a single string rather than an array. `claim_values` normalizes
all of that to a list of strings addressed by a dotted path, so
`mapping.IdentityMappingStore` and `presets.py` can describe *where* a value
lives as plain configuration data instead of per-provider code.

Bounded by construction: the path walk is depth-limited and the returned list
is capped, so a hostile-but-signed token cannot turn a group lookup into
unbounded work. Values are compared, never rendered back to a caller.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

# A dotted claim path deeper than this is not a real IdP layout; refuse to walk
# it rather than follow an attacker-shaped nesting.
MAX_CLAIM_PATH_DEPTH = 8
# Upper bound on how many values one claim contributes to a mapping decision.
# Entra ID caps its own `groups` claim near 200 before switching to a Graph
# reference; 512 leaves generous headroom while staying finite.
MAX_CLAIM_VALUES = 512


def claim_values(claims: Mapping[str, Any], path: str) -> List[str]:
    """The string values at dotted `path` in `claims`, normalized to a list.

    A scalar becomes a one-element list; a list/tuple contributes its string
    elements; anything else (absent, an object, a nested list) contributes
    nothing — so a mapping rule against a claim shaped differently than
    expected fails **closed**, granting nothing, rather than matching loosely.
    """
    node = _walk(claims, path)
    if isinstance(node, str):
        return [node]
    if isinstance(node, bool):
        # bool is an int subclass; treat it as "not a group value" explicitly
        # so `True` can never stringify into a match target.
        return []
    if isinstance(node, (int, float)):
        return [str(node)]
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        values: List[str] = []
        for item in node:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, bool):
                continue
            elif isinstance(item, (int, float)):
                values.append(str(item))
            if len(values) >= MAX_CLAIM_VALUES:
                break
        return values
    return []


def claim_scalar(claims: Mapping[str, Any], path: str) -> str | None:
    """The single string value at dotted `path`, or None.

    Used for identity fields (subject, email, display name) where a list is
    meaningless — a multi-valued claim returns None rather than picking one
    arbitrarily, because silently choosing an element of an array would make
    *which human this is* depend on the IdP's ordering.
    """
    node = _walk(claims, path)
    if isinstance(node, str):
        return node or None
    if isinstance(node, bool):
        return None
    if isinstance(node, (int, float)):
        return str(node)
    return None


def _walk(claims: Mapping[str, Any], path: str) -> Any:
    segments = [segment for segment in path.split(".") if segment]
    if not segments or len(segments) > MAX_CLAIM_PATH_DEPTH:
        return None
    node: Any = claims
    for segment in segments:
        if not isinstance(node, Mapping):
            return None
        node = node.get(segment)
        if node is None:
            return None
    return node
