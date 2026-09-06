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

    # Every artifact that carries the code carries the licence. Under Apache-2.0
    # this is a licence *requirement* (§4(a): recipients must receive a copy),
    # not merely good manners, and `pyproject.toml`'s `license-files` depends on
    # the file existing.
    # The wheel gets it from `License-File` metadata and the sdist from its own
    # root; the container image needs an explicit `COPY LICENSE` in `Dockerfile`,
    # which is asserted here rather than left to a reader noticing its absence.
    if not any(name.endswith("licenses/LICENSE") for name in wheel_members):
        raise SystemExit(
            "release artifact check failed: the wheel carries no LICENSE. Apache-2.0 "
            "\u00a74(a) requires every recipient to get a copy — add `license-files` to "
            "pyproject.toml's [project] table."
        )
    if not any(name.endswith("/LICENSE") for name in sdist_members):
        raise SystemExit("release artifact check failed: the sdist carries no LICENSE")
    dockerfile = (ROOT / "Dockerfile").read_text()
    # The DEFAULT image must ship no proprietary third-party binary. Microsoft's
    # msodbcsql18 lives in the opt-in `production-mssql` target only: Apache-2.0
    # has no third-party pass-through clause, and a publicly pullable image
    # reaches people who never agreed to Microsoft's terms (TODO item 228).
    #
    # Keyed on the INSTALL, not the word: the opt-in stage's own explanatory
    # comment names the package, and a naive substring check over the default
    # slice flagged that comment on the first run.
    _default_stage = dockerfile.split("FROM production AS production-mssql")[0]
    _installs_driver = re.search(r"apt-get install[^\n]*msodbcsql18", _default_stage)
    if _installs_driver:
        raise SystemExit(
            "release artifact check failed: the DEFAULT image installs msodbcsql18. "
            "Microsoft's driver is proprietary and Apache-2.0 carries no pass-through "
            "to bind a downstream puller to its terms — it belongs in the opt-in "
            "`production-mssql` target only. See TODO.md item 228."
        )

    # Where it IS installed, its licence text must land: the slim base image
    # path-excludes /usr/share/doc/*, so the driver's own LICENSE.txt was
    # declared by the package but never shipped. See
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
            "The image is a distribution of the work, so Apache-2.0 \u00a74(a) requires it to "
            "carry the licence text."
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
