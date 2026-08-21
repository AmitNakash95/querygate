"""Trust & evidence page generation and drift guard (TODO.md item 147).

`docs/TRUST_EVIDENCE.md` is a generated, git-committed composition of already-
reviewed evidence docs + the current dependency-audit allowlist status. These
tests make the "always current" claim enforceable: a checked-in doc that
drifted from the generator, or a source doc that went missing, fails here.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from scripts import generate_trust_page

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "TRUST_EVIDENCE.md"
_GENERATED_LINE = re.compile(r"\*Generated (\S+) for QueryGate (\S+)\. ")


def test_committed_doc_matches_generator():
    assert _DOC.exists(), "run `make trust-page` to generate docs/TRUST_EVIDENCE.md"
    committed = _DOC.read_text(encoding="utf-8")
    # Regenerate with the SAME version/date the committed file itself claims
    # (extracted, not re-derived), so this test compares the parts that must
    # be byte-identical without failing merely because it ran on a different
    # day than the last real `make trust-page`.
    match = _GENERATED_LINE.search(committed)
    assert match, "docs/TRUST_EVIDENCE.md's generated-at line has an unexpected shape"
    generated_at, version = match.groups()
    regenerated = generate_trust_page.build_document(version=version, generated_at=generated_at)
    assert (
        committed == regenerated
    ), "docs/TRUST_EVIDENCE.md is stale — regenerate with `make trust-page`"


def test_committed_doc_claims_the_actual_pyproject_version():
    """TODO.md item 147 test-contract gap (found by `test-contract-reviewer`,
    2026-08-05): `test_committed_doc_matches_generator` extracts its
    comparison version FROM the committed file itself, so a version bump in
    `pyproject.toml` with nobody re-running `make trust-page` would pass that
    test green while quietly stamping the wrong package version on a
    customer-facing evidence page."""
    committed = _DOC.read_text(encoding="utf-8")
    match = _GENERATED_LINE.search(committed)
    assert match
    _generated_at, claimed_version = match.groups()
    real_version = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert claimed_version == real_version, (
        f"docs/TRUST_EVIDENCE.md claims QueryGate {claimed_version!r} but "
        f"pyproject.toml's version is {real_version!r} — regenerate with `make trust-page`"
    )


def test_every_source_doc_exists():
    for title, path in generate_trust_page._SOURCES:
        assert path.is_file(), f"trust page source for {title!r} is missing: {path}"


def test_build_document_embeds_every_source_and_the_dependency_summary():
    doc = generate_trust_page.build_document(version="1.2.3", generated_at="2026-01-01")
    assert "QueryGate 1.2.3" in doc
    assert "2026-01-01" in doc
    assert "## Current dependency audit status" in doc
    for title, _path in generate_trust_page._SOURCES:
        assert f"## {title}" in doc


def test_every_relative_link_in_the_generated_page_resolves():
    """The trust packet embeds docs written for three different directories
    (`docs/`, `docs/business/`, and the repo root), so every relative link in a
    source doc has to be repointed at generation time or it 404s for the one
    reader this page exists for. Before `_repoint_links` there were ten dead
    links in the committed page."""
    committed = _DOC.read_text(encoding="utf-8")
    broken = []
    for match in re.finditer(r"\]\(([^)\s]+)\)", committed):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path, _sep, _anchor = target.partition("#")
        if not path:
            continue
        if not (_DOC.parent / path).resolve().exists():
            broken.append(target)
    assert not broken, f"docs/TRUST_EVIDENCE.md has unresolvable links: {sorted(set(broken))}"


def test_repoint_links_rewrites_relative_targets_and_leaves_urls_alone():
    text = (
        "see [a](docs/THREAT_MODEL.md), [b](../INFERENCE_RISKS.md#x), "
        "[c](https://example.test/x), [d](#anchor)"
    )
    rewritten = generate_trust_page._repoint_links(
        text, generate_trust_page.ROOT / "docs" / "business"
    )
    # `docs/business/docs/THREAT_MODEL.md` does not exist, but the rewrite is
    # purely positional and must still be relative to `docs/`, not to the source.
    assert "](business/docs/THREAT_MODEL.md)" in rewritten
    assert "](INFERENCE_RISKS.md#x)" in rewritten
    assert "](https://example.test/x)" in rewritten
    assert "](#anchor)" in rewritten


def test_dependency_summary_reports_zero_when_allowlist_is_empty(tmp_path, monkeypatch):
    empty = tmp_path / "allowlist.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(generate_trust_page, "ALLOWLIST_FILE", empty)
    summary = generate_trust_page._dependency_audit_summary()
    assert "0 allowlisted" in summary


def test_dependency_summary_lists_each_reviewed_entry(tmp_path, monkeypatch):
    populated = tmp_path / "allowlist.json"
    populated.write_text(
        json.dumps(
            [
                {
                    "id": "CVE-2099-0001",
                    "package": "example-pkg",
                    "reason": "not reachable from any code path we call",
                    "added": "2026-01-01",
                    "tracking": "https://example.com/issue/1",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_trust_page, "ALLOWLIST_FILE", populated)
    summary = generate_trust_page._dependency_audit_summary()
    assert "CVE-2099-0001" in summary
    assert "example-pkg" in summary
    assert "1 reviewed allowlist entries" in summary


def test_slug_matches_github_flavored_markdown_anchor_algorithm():
    assert generate_trust_page._slug("Security & reliability posture") == (
        "security--reliability-posture"
    )
