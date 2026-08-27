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


def test_the_package_declares_itself_proprietary():
    classifiers = _pyproject()["project"]["classifiers"]
    assert "License :: Other/Proprietary License" in classifiers, (
        "pyproject.toml no longer declares a proprietary licence classifier; a "
        "scanner or index would read the package as unlicensed (TODO.md item 210)"
    )


def test_the_package_never_declares_an_open_source_licence():
    """The unrecoverable direction: publishing a closed-source product under a
    permissive licence signal is a grant a scanner will propagate.

    Two signals, because checking only the OSI classifier left the stronger one
    open. Under PEP 639 the `license` SPDX expression takes precedence over the
    classifier, so `license = "Apache-2.0"` would give the product away while
    every OSI-classifier assertion stayed green — and `pyproject.toml`'s own
    comment says omitting that key is deliberate.
    """
    project = _pyproject()["project"]
    assert "license" not in project, (
        "pyproject.toml declares an SPDX `license` expression. Its absence is "
        "deliberate (see the comment there): no SPDX identifier means 'proprietary, "
        f"governed by a separate EULA'. Found: {project.get('license')!r}"
    )
    permissive = [
        c
        for c in project["classifiers"]
        if c.startswith("License ::") and c != "License :: Other/Proprietary License"
    ]
    assert not permissive, f"pyproject.toml claims a non-proprietary licence: {permissive}"


def test_the_licence_notice_ships_in_the_distribution():
    """`LICENSE` is the notice pointing at the EULA, and
    `scripts/check_release_artifacts.py` asserts it reaches the wheel, the sdist
    and the image. That chain starts here."""
    assert _pyproject()["project"]["license-files"] == ["LICENSE"]
