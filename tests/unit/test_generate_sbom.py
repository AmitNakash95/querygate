"""Unit tests for the SBOM/dependency-audit release tooling.

These test the deterministic, offline helper functions in
scripts/generate_sbom.py directly (lockfile parsing, allowlist loading, and
the deny-by-default vulnerability evaluation) — not the full script, which
needs network access (pip-audit's vulnerability service) and takes long
enough to build a scratch venv that it belongs in `make release-check`
rather than the default unit suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import generate_sbom

ROOT = Path(__file__).resolve().parents[2]


def test_locked_main_packages_includes_real_runtime_dependencies():
    packages = dict(generate_sbom.locked_main_packages())

    for name in ("mcp", "fastapi", "sqlalchemy", "pydantic", "starlette"):
        assert name in packages, f"expected {name} in poetry.lock's main group"


def test_locked_main_packages_excludes_dev_only_dependencies():
    packages = dict(generate_sbom.locked_main_packages())

    for name in ("pytest", "black", "cyclonedx-bom", "pip-audit"):
        assert name not in packages, f"{name} is dev-only and must not ship"


def test_locked_main_packages_returns_sorted_unique_names():
    packages = generate_sbom.locked_main_packages()
    names = [name for name, _ in packages]

    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_checked_in_allowlist_loads_and_references_real_packages():
    allowlist = generate_sbom.load_allowlist()
    locked_names = {
        generate_sbom._normalize(name) for name, _ in generate_sbom.locked_main_packages()
    }

    # An empty allowlist is the *desired* state: it means every known
    # vulnerability in the shipped dependency set has been remediated (upgraded
    # to a fixed version, or the package removed from the runtime image) rather
    # than accepted with a compensating control (TODO.md item 30 phase 2 /
    # item 89). Any entry that IS present must still reference a currently-locked
    # production dependency — no stale entries for packages we no longer ship.
    for vuln_id, entry in allowlist.items():
        assert entry["id"] == vuln_id
        assert generate_sbom._normalize(entry["package"]) in locked_names, (
            f"allowlist entry {vuln_id!r} references {entry['package']!r}, "
            "which is not (or no longer) a locked production dependency — stale entry?"
        )


def test_load_allowlist_rejects_missing_required_field(tmp_path):
    bad = tmp_path / "allowlist.json"
    bad.write_text(
        json.dumps([{"id": "X-1", "package": "foo", "reason": "r", "added": "2026-01-01"}])
    )

    with pytest.raises(ValueError, match="missing fields"):
        generate_sbom.load_allowlist(bad)


def test_load_allowlist_rejects_duplicate_ids(tmp_path):
    entry = {"id": "X-1", "package": "foo", "reason": "r", "added": "2026-01-01", "tracking": "t"}
    dup = tmp_path / "allowlist.json"
    dup.write_text(json.dumps([entry, entry]))

    with pytest.raises(ValueError, match="duplicate"):
        generate_sbom.load_allowlist(dup)


def _report(entries: dict[str, list[str]]) -> dict:
    return {
        "dependencies": [
            {"name": name, "version": "1.0", "vulns": [{"id": vid} for vid in vuln_ids]}
            for name, vuln_ids in entries.items()
        ]
    }


def test_evaluate_findings_flags_unreviewed_vulnerabilities():
    report = _report({"click": ["PYSEC-NEW-1"]})

    unreviewed = generate_sbom._evaluate_findings(report, allowlist={})

    assert len(unreviewed) == 1
    assert "PYSEC-NEW-1" in unreviewed[0]


def test_evaluate_findings_accepts_allowlisted_vulnerabilities():
    report = _report({"click": ["PYSEC-2026-2132"]})
    allowlist = {"PYSEC-2026-2132": {"id": "PYSEC-2026-2132", "package": "click"}}

    assert generate_sbom._evaluate_findings(report, allowlist) == []


def test_evaluate_findings_ignores_venv_bootstrap_packages():
    report = _report({"pip": ["PYSEC-BOOTSTRAP-1"], "setuptools": ["PYSEC-BOOTSTRAP-2"]})

    assert generate_sbom._evaluate_findings(report, allowlist={}) == []


def test_evaluate_findings_partial_allowlist_still_flags_the_rest():
    report = _report({"starlette": ["A-1", "A-2"]})
    allowlist = {"A-1": {"id": "A-1", "package": "starlette"}}

    unreviewed = generate_sbom._evaluate_findings(report, allowlist)

    assert len(unreviewed) == 1
    assert "A-2" in unreviewed[0]
