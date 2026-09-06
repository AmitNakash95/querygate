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
import tomllib
from pathlib import Path

import pytest

from scripts import check_licenses, generate_sbom

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


def test_license_report_is_a_release_artifact():
    """`make sbom` copies docs/THIRD_PARTY_LICENSES.md into dist/ and covers it
    with SHA256SUMS, so a consumer verifying a downloaded bundle gets the
    third-party licence inventory with it rather than having to be sent it."""
    assert generate_sbom.LICENSE_REPORT.is_file()
    assert generate_sbom.LICENSE_REPORT.name == "THIRD_PARTY_LICENSES.md"


def test_checksum_manifest_covers_every_artifact_it_is_given(tmp_path):
    paths = []
    for name in ("a.whl", "b.tar.gz", "c.cdx.json", "THIRD_PARTY_LICENSES.md"):
        path = tmp_path / name
        path.write_text(name)
        paths.append(path)
    manifest = tmp_path / "SHA256SUMS"
    generate_sbom._write_checksums(paths, manifest)
    listed = {line.split("  ", 1)[1] for line in manifest.read_text().splitlines()}
    assert listed == {p.name for p in paths}


def _stub_artifacts(tmp_path):
    for name in ("q.whl", "q.tar.gz", "q.cdx.json"):
        (tmp_path / name).write_text(name)
    return tmp_path / "q.whl", tmp_path / "q.tar.gz", tmp_path / "q.cdx.json"


def test_staged_release_artifacts_include_the_licence_inventory(tmp_path):
    """The actual wiring: what `main` hands to `_write_checksums` must contain the
    licence report, copied with the real content. Dropping either the copy or the
    list entry would leave a consumer verifying a bundle that has no inventory —
    and `verify_release.py` cannot notice, since it only checks what the manifest
    names."""
    wheel, sdist, sbom = _stub_artifacts(tmp_path)
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    report.write_text("# Third-party licences\n")
    dist = tmp_path / "dist"
    dist.mkdir()

    staged = generate_sbom.stage_release_artifacts(
        wheel, sdist, sbom, dist=dist, license_report=report, gate_command=["true"]
    )

    assert [p.name for p in staged] == [
        "q.whl",
        "q.tar.gz",
        "q.cdx.json",
        "THIRD_PARTY_LICENSES.md",
    ]
    assert (dist / "THIRD_PARTY_LICENSES.md").read_text() == "# Third-party licences\n"


def test_a_missing_licence_inventory_stops_the_release(tmp_path):
    wheel, sdist, sbom = _stub_artifacts(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(SystemExit, match="make license-report"):
        generate_sbom.stage_release_artifacts(
            wheel,
            sdist,
            sbom,
            dist=dist,
            license_report=tmp_path / "absent.md",
            gate_command=["true"],
        )


def test_a_stale_licence_inventory_is_not_shipped(tmp_path, monkeypatch):
    """`make sbom` standalone does not run license-check first, so this does."""
    wheel, sdist, sbom = _stub_artifacts(tmp_path)
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    report.write_text("stale\n")
    dist = tmp_path / "dist"
    dist.mkdir()

    class _Failed:
        returncode = 1
        stdout = "is out of date with poetry.lock"
        stderr = ""

    invoked = []

    def fake_run(command, **kwargs):
        invoked.append(command)
        return _Failed()

    monkeypatch.setattr(generate_sbom.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="stale or failing its own gate"):
        generate_sbom.stage_release_artifacts(wheel, sdist, sbom, dist=dist, license_report=report)
    assert not (dist / "THIRD_PARTY_LICENSES.md").exists()
    # It must run the licence gate in CHECK mode. Running `--write` here would
    # regenerate the report instead of gating it — masking the very staleness this
    # rule exists to catch, and rewriting a tracked file from inside the suite.
    assert len(invoked) == 1
    assert any("check_licenses.py" in str(part) for part in invoked[0])
    assert "--check" in invoked[0] and "--write" not in invoked[0]


def test_main_hands_the_staged_artifacts_to_the_checksum_manifest(tmp_path, monkeypatch):
    """The wiring the helper tests cannot see: `main` must checksum what
    `stage_release_artifacts` returns. Reverting that one line drops the licence
    inventory from SHA256SUMS with every other test green — and
    `scripts/verify_release.py` cannot notice, since it only verifies the files
    the manifest names."""
    dist = tmp_path / "dist"
    dist.mkdir()
    version = tomllib.loads((generate_sbom.ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    wheel = dist / f"querygate-{version}-py3-none-any.whl"
    sdist = dist / f"querygate-{version}.tar.gz"
    for path in (wheel, sdist):
        path.write_text(path.name)
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    report.write_text("# Third-party licences\n")

    monkeypatch.setattr(generate_sbom, "DIST", dist)
    monkeypatch.setattr(generate_sbom, "LICENSE_REPORT", report)
    monkeypatch.setattr(generate_sbom, "LICENSE_GATE_COMMAND", ["true"])
    monkeypatch.setattr(generate_sbom, "_build_locked_venv", lambda *a, **k: tmp_path / "py")
    monkeypatch.setattr(
        generate_sbom,
        "_generate_cyclonedx_sbom",
        lambda venv_python, output, ver: (output.write_text("{}"), {"components": []})[1],
    )
    monkeypatch.setattr(
        generate_sbom, "_audit_dependencies", lambda venv_dir, output: {"dependencies": []}
    )

    generate_sbom.main()

    # The staged copy must land in the patched dist/, not the repo's — an
    # early-bound default here previously wrote into the real one.
    assert (dist / "THIRD_PARTY_LICENSES.md").is_file()
    # Compare RESOLVED PATHS, not inodes. `Path.samefile()` stats both sides and
    # raises FileNotFoundError when either is missing — and the repo's own
    # dist/SHA256SUMS only exists on a machine that has previously run a build,
    # since dist/ is gitignored. That made this test pass locally and fail in
    # every clean CI checkout with a FileNotFoundError that looked nothing like
    # the assertion it came from. Path comparison expresses the same intent
    # ("a different file") and needs neither side to exist.
    assert (dist / "SHA256SUMS").is_file()
    assert (dist / "SHA256SUMS").resolve() != (generate_sbom.ROOT / "dist" / "SHA256SUMS").resolve()
    listed = {line.split("  ", 1)[1] for line in (dist / "SHA256SUMS").read_text().splitlines()}
    assert "THIRD_PARTY_LICENSES.md" in listed
    assert listed == {wheel.name, sdist.name, f"querygate-{version}.cdx.json", report.name}
