#!/usr/bin/env python3
"""Generate a CycloneDX SBOM, a dependency vulnerability report, and a
SHA-256 checksum manifest for the exact production dependency set QueryGate
ships — the `main` group of `poetry.lock`, not whatever a fresh unpinned
resolve would pick.

Why a throwaway venv instead of the repo's own `.venv`: this project's own
`.venv` also has the `dev` dependency group installed (pytest, black, this
script's own SBOM/audit tooling, ...), which would pollute both the SBOM and
the vulnerability report with packages nobody ships. Building a scratch venv
from `poetry.lock`'s `main`-group pins, `--no-deps`, reproduces exactly what
`poetry install --only main` would install — the same lockfile the test
suite runs against — without disturbing the working dev environment.

Every non-allowlisted known vulnerability in that dependency set fails this
script (deny-by-default). Already-reviewed findings are recorded, with a
reason and follow-up reference, in `security/dependency-audit-allowlist.json`
— reviewed and tracked, not silently ignored.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "poetry.lock"
ALLOWLIST_FILE = ROOT / "security" / "dependency-audit-allowlist.json"
# The third-party licence inventory (scripts/check_licenses.py) is checked into
# docs/ for review, but a consumer who downloads a release bundle needs it too —
# it is the standard answer to the "list your third-party components and their
# licences" question. Copying it into dist/ and covering it with SHA256SUMS makes
# it a first-class release artifact alongside the SBOM, rather than a repo file a
# reviewer has to be sent separately. The path is imported rather than restated so
# the two scripts cannot drift apart about which file that is.
from scripts.check_licenses import REPORT_FILE as LICENSE_REPORT  # noqa: E402

DIST = ROOT / "dist"

# Bootstrapped into every fresh venv by ensurepip; not a querygate dependency
# and not in poetry.lock's `main` group — excluded from both the SBOM and
# the vulnerability report so neither describes packages nobody ships.
VENV_BOOTSTRAP_PACKAGES = frozenset({"pip", "setuptools", "wheel"})


def _display(path: Path) -> str:
    """Repo-relative path for messages, tolerating a path outside the repo (a
    test pointing `DIST` at a tmp dir, or a caller building elsewhere)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def locked_main_packages(
    lock_path: Path = LOCK_FILE, environment: dict | None = None
) -> list[tuple[str, str]]:
    """Return (name, version) pairs for poetry.lock's `main` group, filtered
    to packages whose markers apply on this platform/interpreter.

    `environment` overrides "this platform" — pass
    `check_licenses.IMAGE_ENVIRONMENT` to ask what the *published image* gets.
    Without it, this answers for the machine running the build, which is the
    right question for an SBOM describing the environment it just built."""
    from packaging.markers import Marker

    data = tomllib.loads(lock_path.read_text())
    packages: list[tuple[str, str]] = []
    for pkg in data["package"]:
        if "main" not in pkg.get("groups", []):
            continue
        marker = pkg.get("markers")
        if isinstance(marker, dict):
            marker = marker.get("main")
        if marker:
            parsed = Marker(marker)
            applies = parsed.evaluate(environment) if environment else parsed.evaluate()
            if not applies:
                continue
        packages.append((pkg["name"], pkg["version"]))
    return sorted(packages)


def load_allowlist(path: Path = ALLOWLIST_FILE) -> dict[str, dict]:
    """Load the checked-in, reviewed vulnerability allowlist, keyed by
    advisory id. Raises on malformed entries so a bad edit fails loudly
    rather than silently widening (or narrowing) the ignore set."""
    entries = json.loads(path.read_text())
    required = {"id", "package", "reason", "added", "tracking"}
    by_id: dict[str, dict] = {}
    for entry in entries:
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"dependency audit allowlist entry missing fields {missing}: {entry}")
        if entry["id"] in by_id:
            raise ValueError(f"duplicate allowlist entry for {entry['id']!r}")
        by_id[entry["id"]] = entry
    return by_id


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _build_locked_venv(venv_dir: Path, wheel: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python"

    packages = locked_main_packages()
    requirements = venv_dir / "locked-main-requirements.txt"
    requirements.write_text("\n".join(f"{name}=={version}" for name, version in packages) + "\n")

    _run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", "-r", str(requirements)])
    _run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheel)])
    check = subprocess.run([str(python), "-m", "pip", "check"], capture_output=True, text=True)
    if check.returncode != 0:
        raise SystemExit(f"locked dependency set is inconsistent:\n{check.stdout}{check.stderr}")
    return python


def _generate_cyclonedx_sbom(venv_python: Path, output: Path, version: str) -> dict:
    _run(
        [
            sys.executable,
            "-m",
            "cyclonedx_py",
            "environment",
            "--of",
            "JSON",
            "-o",
            str(output),
            str(venv_python),
        ]
    )
    sbom = json.loads(output.read_text())
    allowed = {_normalize(name) for name, _ in locked_main_packages()} | {"querygate"}
    sbom["components"] = [c for c in sbom.get("components", []) if _normalize(c["name"]) in allowed]
    # cyclonedx-py's poetry/pyproject root-component reading assumes a
    # legacy [tool.poetry] name/version table; this project's pyproject.toml
    # is PEP 621 (`[project]`), so the root component is set explicitly here
    # instead of relying on that (broken, for this layout) auto-detection.
    sbom["metadata"]["component"] = {
        "type": "application",
        "name": "querygate",
        "version": version,
        "purl": f"pkg:pypi/querygate@{version}",
    }
    output.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n")
    return sbom


def _audit_dependencies(venv_dir: Path, output: Path) -> dict:
    # pip-audit exits 1 (not 0) when it successfully finds vulnerabilities —
    # that is the expected, common case here, not a tool failure. Only a
    # missing/invalid output file indicates the run itself broke.
    site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--path",
            str(site_packages),
            "--format",
            "json",
            "--desc",
            "on",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    if not output.is_file():
        raise SystemExit(f"pip-audit did not produce a report at {output}")
    return json.loads(output.read_text())


def _evaluate_findings(report: dict, allowlist: dict[str, dict]) -> list[str]:
    """Return human-readable lines for every finding not covered by the
    allowlist. An empty return means the audit passes."""
    unreviewed: list[str] = []
    for dep in report["dependencies"]:
        if dep["name"] in VENV_BOOTSTRAP_PACKAGES:
            continue
        for vuln in dep.get("vulns", []):
            if vuln["id"] in allowlist:
                continue
            unreviewed.append(f"{dep['name']} {dep['version']}: {vuln['id']} (not in allowlist)")
    return unreviewed


def _write_checksums(paths: list[Path], output: Path) -> None:
    import hashlib

    lines = []
    for path in sorted(paths):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    output.write_text("\n".join(lines) + "\n")


LICENSE_GATE_COMMAND = [sys.executable, str(ROOT / "scripts" / "check_licenses.py"), "--check"]


def stage_release_artifacts(
    wheel: Path,
    sdist: Path,
    sbom_path: Path,
    dist: Path | None = None,
    license_report: Path | None = None,
    gate_command: list[str] | None = None,
) -> list[Path]:
    """Copy the licence inventory into `dist/` and return everything SHA256SUMS covers.

    Extracted from `main` so the set of checksummed artifacts is testable: an
    artifact silently dropped from this list would be invisible to
    `scripts/verify_release.py`, which only verifies what the manifest names.
    """
    # Every default is resolved here, not in the signature: a default argument is
    # bound once at import, so `dist: Path = DIST` would ignore a caller (or a
    # test) that rebinds the module attribute — and would then write into the
    # real dist/ while appearing hermetic. `make verify-release` caught exactly
    # that, which is the behaviour it exists for.
    dist = dist if dist is not None else DIST
    report = license_report if license_report is not None else LICENSE_REPORT
    if not report.is_file():
        raise SystemExit(f"{_display(report)} is missing — run `make license-report`")
    # Existence is not enough: a stale report would be copied into dist/ and signed
    # into SHA256SUMS, so a consumer would verify the integrity of the wrong answer.
    # `make release-check` runs license-check before this, but `make sbom` on its own
    # does not, so check here rather than relying on the caller's ordering.
    license_check = subprocess.run(
        gate_command if gate_command is not None else LICENSE_GATE_COMMAND,
        capture_output=True,
        text=True,
    )
    if license_check.returncode != 0:
        raise SystemExit(
            "the third-party licence inventory is stale or failing its own gate, so it "
            "must not be shipped:\n" + license_check.stdout + license_check.stderr
        )
    license_copy = dist / report.name
    license_copy.write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Third-party licences -> {license_copy}")
    return [wheel, sdist, sbom_path, license_copy]


def main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = DIST / f"querygate-{version}-py3-none-any.whl"
    sdist = DIST / f"querygate-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise SystemExit(f"release artifacts are missing — run `poetry build` first ({wheel})")

    allowlist = load_allowlist()

    with tempfile.TemporaryDirectory(prefix="querygate-sbom-") as tmp:
        venv_dir = Path(tmp) / "locked-venv"
        venv_python = _build_locked_venv(venv_dir, wheel)

        sbom_path = DIST / f"querygate-{version}.cdx.json"
        sbom = _generate_cyclonedx_sbom(venv_python, sbom_path, version)
        print(f"SBOM: {len(sbom['components'])} components -> {_display(sbom_path)}")

        audit_path = DIST / f"querygate-{version}.vuln-report.json"
        report = _audit_dependencies(venv_dir, audit_path)
        print(f"Dependency audit report -> {_display(audit_path)}")

    unreviewed = _evaluate_findings(report, allowlist)
    if unreviewed:
        raise SystemExit(
            "dependency audit found vulnerabilities with no reviewed allowlist entry:\n- "
            + "\n- ".join(unreviewed)
            + f"\n\nReview each finding, then either upgrade the dependency or add a "
            f"justified entry to {_display(ALLOWLIST_FILE)}."
        )
    print(f"Dependency audit: no unreviewed known vulnerabilities ({len(allowlist)} allowlisted).")

    checksummed = stage_release_artifacts(wheel, sdist, sbom_path)
    _write_checksums(checksummed, DIST / "SHA256SUMS")
    print(f"Checksums -> {_display(DIST / 'SHA256SUMS')}")


if __name__ == "__main__":
    main()
