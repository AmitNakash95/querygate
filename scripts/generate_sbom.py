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
DIST = ROOT / "dist"

# Bootstrapped into every fresh venv by ensurepip; not a querygate dependency
# and not in poetry.lock's `main` group — excluded from both the SBOM and
# the vulnerability report so neither describes packages nobody ships.
VENV_BOOTSTRAP_PACKAGES = frozenset({"pip", "setuptools", "wheel"})


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def locked_main_packages(lock_path: Path = LOCK_FILE) -> list[tuple[str, str]]:
    """Return (name, version) pairs for poetry.lock's `main` group, filtered
    to packages whose markers apply on this platform/interpreter."""
    from packaging.markers import Marker

    data = tomllib.loads(lock_path.read_text())
    packages: list[tuple[str, str]] = []
    for pkg in data["package"]:
        if "main" not in pkg.get("groups", []):
            continue
        marker = pkg.get("markers")
        if isinstance(marker, dict):
            marker = marker.get("main")
        if marker and not Marker(marker).evaluate():
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
        print(f"SBOM: {len(sbom['components'])} components -> {sbom_path.relative_to(ROOT)}")

        audit_path = DIST / f"querygate-{version}.vuln-report.json"
        report = _audit_dependencies(venv_dir, audit_path)
        print(f"Dependency audit report -> {audit_path.relative_to(ROOT)}")

    unreviewed = _evaluate_findings(report, allowlist)
    if unreviewed:
        raise SystemExit(
            "dependency audit found vulnerabilities with no reviewed allowlist entry:\n- "
            + "\n- ".join(unreviewed)
            + f"\n\nReview each finding, then either upgrade the dependency or add a "
            f"justified entry to {ALLOWLIST_FILE.relative_to(ROOT)}."
        )
    print(f"Dependency audit: no unreviewed known vulnerabilities ({len(allowlist)} allowlisted).")

    _write_checksums([wheel, sdist, sbom_path], DIST / "SHA256SUMS")
    print(f"Checksums -> {(DIST / 'SHA256SUMS').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
