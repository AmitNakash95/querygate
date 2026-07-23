"""Scope-catalog completeness and drift guards (TODO.md item 95).

`core/scopes.py` is the single source of truth for the authorization vocabulary;
the RFC 9728 metadata and `docs/SCOPE_CATALOG.md` both derive from it. These
tests make that guarantee enforceable: a scope constant that isn't catalogued,
a bundle referencing an unknown scope, or a committed doc that drifted from the
generator all fail here — so the "forgotten scope" gap the item closes cannot
silently reopen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from querygate.core import scopes as scopes_mod
from querygate.core.scopes import ALL_SCOPES, ROLE_BUNDLES, SCOPE_CATALOG
from querygate.scope_catalog import render_scope_catalog_markdown

pytestmark = pytest.mark.unit

_DOC = Path(__file__).resolve().parents[2] / "docs" / "SCOPE_CATALOG.md"


def _scope_constants() -> set[str]:
    """Every `*_SCOPE` string constant declared in core/scopes.py."""
    return {
        value
        for name, value in vars(scopes_mod).items()
        if name.endswith("_SCOPE") and isinstance(value, str)
    }


def test_every_scope_constant_is_catalogued_exactly_once():
    catalog_scopes = [info.scope for info in SCOPE_CATALOG]
    assert len(catalog_scopes) == len(set(catalog_scopes)), "duplicate scope in SCOPE_CATALOG"
    assert set(catalog_scopes) == _scope_constants(), (
        "SCOPE_CATALOG must list every *_SCOPE constant and nothing else — add the "
        "new scope's ScopeInfo row when you add its constant"
    )


def test_all_scopes_matches_catalog_order():
    assert ALL_SCOPES == tuple(info.scope for info in SCOPE_CATALOG)


def test_bundles_reference_only_known_scopes_and_cover_every_scope():
    known = set(ALL_SCOPES)
    covered: set[str] = set()
    for bundle in ROLE_BUNDLES:
        assert len(bundle.scopes) == len(set(bundle.scopes)), f"{bundle.name} has duplicate scopes"
        for scope in bundle.scopes:
            assert scope in known, f"{bundle.name} references unknown scope {scope!r}"
            covered.add(scope)
    # Every privileged scope should appear in at least one recommended bundle, so
    # an operator wiring roles from this catalog can grant all of them.
    assert covered == known, f"scopes missing from every bundle: {sorted(known - covered)}"


def test_committed_doc_matches_generator():
    assert _DOC.exists(), "run `make scope-catalog` to generate docs/SCOPE_CATALOG.md"
    assert (
        _DOC.read_text(encoding="utf-8") == render_scope_catalog_markdown()
    ), "docs/SCOPE_CATALOG.md is stale — regenerate with `make scope-catalog`"
