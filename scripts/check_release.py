#!/usr/bin/env python3
"""Fail fast when the source tree is not suitable for a QueryGate release."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "CHANGELOG.md",
    "README.md",
    "docs/RELEASING.md",
    "Dockerfile",
    "poetry.lock",
    "pyproject.toml",
)
LEGACY_IDENTITIES = ("il-backoffice", "il_backoffice", "shany", "grammarql")
PRODUCT_PATHS = (
    ".env.example",
    ".github",
    "Dockerfile",
    "Makefile",
    "README.md",
    "docker-compose.yml",
    "examples",
    "scripts",
    "src",
    "tests",
)


def _tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=False).decode()
    return [path for path in output.split("\0") if path]


def _source_version() -> str:
    source = (ROOT / "src/querygate/__init__.py").read_text()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise SystemExit("release check failed: querygate.__version__ is missing")
    return match.group(1)


def main() -> None:
    failures: list[str] = []
    for path in REQUIRED_FILES:
        if not (ROOT / path).is_file():
            failures.append(f"required release file is missing: {path}")

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = metadata["project"]["version"]
    source_version = _source_version()
    if project_version != source_version:
        failures.append(
            f"version mismatch: pyproject={project_version!r}, source={source_version!r}"
        )

    tracked = _tracked_files()
    forbidden = [
        path
        for path in tracked
        if path == ".env"
        or path.startswith(("dist/", "build/", ".venv/"))
        or "/__pycache__/" in f"/{path}/"
        or path.endswith((".db", ".sqlite", ".sqlite3"))
    ]
    if forbidden:
        failures.append("generated or secret files are tracked: " + ", ".join(forbidden))

    for relative in PRODUCT_PATHS:
        path = ROOT / relative
        candidates = [path] if path.is_file() else list(path.rglob("*")) if path.exists() else []
        for candidate in candidates:
            if (
                not candidate.is_file()
                or candidate == Path(__file__).resolve()
                or candidate.suffix in {".db", ".png", ".svg"}
            ):
                continue
            try:
                text = candidate.read_text().lower()
            except UnicodeDecodeError:
                continue
            matches = [identity for identity in LEGACY_IDENTITIES if identity in text]
            if matches:
                failures.append(
                    f"legacy identity {matches!r} remains in {candidate.relative_to(ROOT)}"
                )

    if failures:
        raise SystemExit("release check failed:\n- " + "\n- ".join(failures))
    print(f"release source checks passed for QueryGate {project_version}")


if __name__ == "__main__":
    main()
