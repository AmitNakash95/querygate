#!/usr/bin/env python3
"""Verify the built wheel and source distribution contain only release inputs."""

from __future__ import annotations

import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pyc")
# "demo" carries the partner-demo stack, including a deliberately unsafe
# execute_sql MCP server that exists only as the counter-example QueryGate
# replaces (demo/baseline_mcp/). It must never reach a release artifact.
#: `control-plane` is here (item 212) as the cheapest possible guarantee that
#: the vendor signing plane can never ship inside a customer artifact. The
#: Dockerfile's COPY allowlist already makes the image safe; this covers the
#: wheel and the sdist, and survives a Dockerfile edit.
FORBIDDEN_PARTS = {
    ".env",
    ".git",
    ".venv",
    "__pycache__",
    "archive",
    "tests",
    "demo",
    "control-plane",
}


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

    # Every artifact that carries the code carries the licence notice. Under the
    # cancelled BSL plan this was a licence *requirement*; under the proprietary
    # EULA (item 210) it is what puts the terms in front of whoever installs the
    # package, and `pyproject.toml`'s `license-files` depends on the file existing.
    # The wheel gets it from `License-File` metadata and the sdist from its own
    # root; the container image needs an explicit `COPY LICENSE` in `Dockerfile`,
    # which is asserted here rather than left to a reader noticing its absence.
    if not any(name.endswith("licenses/LICENSE") for name in wheel_members):
        raise SystemExit(
            "release artifact check failed: the wheel carries no LICENSE. QueryGate ships "
            "proprietary and every artifact must carry the notice — add `license-files` to "
            "pyproject.toml's [project] table."
        )
    if not any(name.endswith("/LICENSE") for name in sdist_members):
        raise SystemExit("release artifact check failed: the sdist carries no LICENSE")
    dockerfile = (ROOT / "Dockerfile").read_text()
    # msodbcsql18 is proprietary and its EULA is declared by the package but
    # path-excluded by the slim base image, so it silently did not ship. See
    # docs/CONTAINER_IMAGE_LICENCES.md (TODO item 196).
    if "msodbcsql18" in dockerfile and "path-include /usr/share/doc/msodbcsql18" not in dockerfile:
        raise SystemExit(
            "release artifact check failed: Dockerfile installs msodbcsql18 without the "
            "dpkg path-include that keeps its EULA in the image. The slim base image "
            "path-excludes /usr/share/doc/*, so the driver would ship with no licence text."
        )
    if not re.search(r"^COPY\s+LICENSE\b", dockerfile, re.M):
        raise SystemExit(
            "release artifact check failed: Dockerfile does not COPY LICENSE into the image. "
            "The image is how QueryGate is distributed, so it must carry the proprietary "
            "notice and the pointer to docs/legal/EULA.en.md."
        )

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
