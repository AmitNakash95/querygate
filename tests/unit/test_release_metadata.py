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

from querygate import __version__
from querygate.core.config import AppConfig

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


def test_the_parsed_date_value_is_asserted_not_just_its_presence():
    """Narrowing the date group to `(\\d{4})` would otherwise stay green: the
    *requirement* of a date was pinned, the *parse* was not."""
    assert release_dates()["0.1.0"] == "2026-07-18"


def test_the_detector_would_catch_an_undated_version():
    """Negative control: the parser must not match a heading with no date."""
    assert not _RELEASE_HEADING.search("## [9.9.9]\n")
    assert not _RELEASE_HEADING.search("## [9.9.9] — unreleased\n")
    assert _RELEASE_HEADING.search("## [9.9.9] — 2026-01-02\n")


# --- restored coverage --------------------------------------------------------
#
# These two tests predate the licensing work and had nothing to do with the BSL
# Change Date. They were destroyed when this file was rewritten wholesale to
# rescue the changelog binding out of the deleted `test_change_date.py` — the
# rescue was selective rather than systematic, and this file's own path already
# existed. Restored verbatim.
#
# `test_default_example_config_paths_work_outside_repository_cwd` matters more
# than it looks: `AppConfig`'s defaults resolve through
# `importlib.resources.files("examples")`, and that is exactly the mechanism a
# frozen/zipped distribution breaks. It is the only guard of that property in
# the suite — deleted, ironically, in the same branch that adopts PyInstaller
# freezing (TODO.md item 214).


def test_runtime_version_matches_application_default():
    assert AppConfig(_env_file=None).app_version == __version__


def test_default_example_config_paths_work_outside_repository_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = AppConfig(_env_file=None)

    assert Path(config.connections_file).is_file()
    assert Path(config.policy_file).is_file()
    assert Path(config.connections_file).name == "connections.example.yaml"
    assert Path(config.policy_file).name == "policy.example.yaml"


# --- the licence classifier (item 210) ----------------------------------------
#
# `pyproject.toml`'s own comment calls the classifier the load-bearing signal:
# "what every scanner, index and SBOM consumer actually reads". It shipped with
# no test at all, so deleting the line left the suite green and the package
# publishing with no licence signal on a product whose entire positioning is
# proprietary. That is the "guard that cannot fail" shape this file exists for.


def _pyproject() -> dict:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_declares_apache_2_0_in_both_places():
    """Both licence signals must agree, and both must say Apache-2.0.

    Two signals because either one alone is readable in isolation and they can
    disagree: under PEP 639 the `license` SPDX expression takes precedence over
    the classifier, so a stale `License :: Other/Proprietary License` classifier
    beside `license = "Apache-2.0"` would leave scanners split on whether the
    package may be used at all. This is the mirror of the guard it replaces —
    that one existed to stop a permissive signal leaking into a proprietary
    product; this one exists to stop a proprietary signal surviving into an open
    one, which is the direction that now costs adoption.
    """
    project = _pyproject()["project"]
    assert project.get("license") == "Apache-2.0", (
        'pyproject.toml must declare `license = "Apache-2.0"` (PEP 639). '
        f"Found: {project.get('license')!r}"
    )
    classifiers = project["classifiers"]
    assert "License :: OSI Approved :: Apache Software License" in classifiers, (
        "pyproject.toml no longer declares the Apache-2.0 classifier; an index "
        "would read the package as unlicensed"
    )
    stale = [c for c in classifiers if c == "License :: Other/Proprietary License"]
    assert not stale, (
        "pyproject.toml still carries the proprietary classifier alongside the "
        "Apache-2.0 SPDX expression; the two signals contradict each other"
    )


def test_the_license_file_is_the_real_apache_text():
    """A LICENSE that merely says "Apache-2.0" grants nothing.

    The grant lives in the text, so this pins the operative clauses rather than
    the file's name — a truncated or placeholder LICENSE would otherwise pass
    every other assertion here.
    """
    text = (ROOT / "LICENSE").read_text()
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    for clause in (
        "2. Grant of Copyright License.",
        "3. Grant of Patent License.",
        "7. Disclaimer of Warranty.",
    ):
        assert clause in text, f"LICENSE is missing Apache-2.0 clause: {clause}"
    assert (
        "All rights reserved" not in text
    ), "LICENSE still contains proprietary reservation language"


def test_the_licence_notice_ships_in_the_distribution():
    """`LICENSE` carries the Apache-2.0 grant, and
    `scripts/check_release_artifacts.py` asserts it reaches the wheel, the sdist
    and the image. That chain starts here."""
    assert _pyproject()["project"]["license-files"] == ["LICENSE"]
