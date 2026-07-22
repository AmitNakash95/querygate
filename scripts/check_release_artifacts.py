#!/usr/bin/env python3
"""Verify the built wheel and source distribution contain only release inputs."""

from __future__ import annotations

import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pyc")
FORBIDDEN_PARTS = {".env", ".git", ".venv", "__pycache__", "archive", "tests"}


def _invalid_members(members: list[str]) -> list[str]:
    invalid: list[str] = []
    for member in members:
        parts = set(Path(member).parts)
        if parts & FORBIDDEN_PARTS or member.endswith(FORBIDDEN_SUFFIXES):
            invalid.append(member)
    return invalid


def main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = DIST / f"querygate-{version}-py3-none-any.whl"
    sdist = DIST / f"querygate-{version}.tar.gz"
    missing = [str(path.relative_to(ROOT)) for path in (wheel, sdist) if not path.is_file()]
    if missing:
        raise SystemExit("release artifact check failed: missing " + ", ".join(missing))

    with zipfile.ZipFile(wheel) as archive:
        wheel_members = archive.namelist()
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_members = archive.getnames()

    invalid = _invalid_members(wheel_members + sdist_members)
    if invalid:
        raise SystemExit("release artifact check failed: forbidden members: " + ", ".join(invalid))

    required_wheel = {
        "querygate/__init__.py",
        "examples/connections.example.yaml",
        "examples/policy.example.yaml",
        "examples/catalog.example.yaml",
        "examples/catalog_drafts.example.yaml",
        "querygate/help/content/manifest.yaml",
        "querygate/help/content/architecture.md",
        "querygate/help/content/authentication.md",
        "querygate/help/content/catalog.md",
        "querygate/help/content/connections.md",
        "querygate/help/content/first_run.md",
        "querygate/help/content/governance.md",
        "querygate/help/content/interfaces.md",
        "querygate/help/content/observability.md",
        "querygate/help/content/troubleshooting.md",
        "querygate/help/content/upgrades.md",
        "querygate/catalog/benchmark_data/semantic_memory_v1.yaml",
    }
    absent = sorted(required_wheel - set(wheel_members))
    if absent:
        raise SystemExit("release artifact check failed: wheel is missing " + ", ".join(absent))

    print(
        f"release artifacts passed for QueryGate {version}: "
        f"{len(wheel_members)} wheel files, {len(sdist_members)} sdist files"
    )


if __name__ == "__main__":
    main()
