"""Release metadata bindings that no other test asserts.

`tests/unit/test_change_date.py` was deleted with the BSL apparatus (TODO.md
item 210). One assertion in it was not about the Change Date at all and had no
other home: **the pyproject version must have a dated CHANGELOG entry.**
`scripts/check_release.py` binds `pyproject.toml` to `__init__.py`, and nothing
bound either to `CHANGELOG.md` — so a version bump with no changelog entry was
green until release time.

Preserved here rather than dropped, because the failure it catches (shipping a
version nobody wrote release notes for) survives the licence change entirely.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# `## [0.1.0] — 2026-07-18` (em dash or hyphen, both seen in the wild).
_RELEASE_HEADING = re.compile(
    r"^##\s*\[(?P<version>[^\]]+)\]\s*[—-]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$",
    re.MULTILINE,
)


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]


def release_dates() -> dict[str, str]:
    text = (ROOT / "CHANGELOG.md").read_text("utf-8")
    return {m.group("version"): m.group("date") for m in _RELEASE_HEADING.finditer(text)}


def test_changelog_release_headings_are_parseable():
    """The binding below is only meaningful if the heading format holds."""
    dates = release_dates()
    assert dates, (
        "no `## [version] — YYYY-MM-DD` heading found in CHANGELOG.md; either the "
        "file changed format or the pattern here has drifted"
    )


def test_the_current_version_has_a_dated_changelog_entry():
    """Binds pyproject to CHANGELOG.

    Without it, a version bump with no changelog entry stays green through the
    whole suite and is only noticed when someone goes to cut the release.
    """
    version = project_version()
    dates = release_dates()
    assert version in dates, (
        f"pyproject version {version} has no `## [{version}] — YYYY-MM-DD` heading in "
        f"CHANGELOG.md; known versions: {sorted(dates)}"
    )


def test_the_detector_would_catch_an_undated_version():
    """Negative control: the parser must not match a heading with no date."""
    assert not _RELEASE_HEADING.search("## [9.9.9]\n")
    assert not _RELEASE_HEADING.search("## [9.9.9] — unreleased\n")
    assert _RELEASE_HEADING.search("## [9.9.9] — 2026-01-02\n")
