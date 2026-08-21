"""BSL Change Date gate — `scripts/check_change_date.py` / `LICENSE`.

"4 years per release" is an arithmetic promise about a legal document, and the
failure mode is silent: a release that ships the previous release's Change Date
gives away four years of grant, or takes them back, and nothing about the file
looks wrong. So the date is *derived* from CHANGELOG's release date rather than
typed, and the gate recomputes it on every run.

Same shape as the other worklist/licence guards: a live half that pins the real
repository, and detector tests that plant drift and require each rule to fire.
The live half is deliberately written to work in both states — today `LICENSE`
is an unstamped draft, and the gate must be meaningful *now* rather than
becoming meaningful only after the flip.
"""

from __future__ import annotations

import sys
from datetime import date

import pytest

from scripts import check_change_date

pytestmark = pytest.mark.unit


STAMPED = """Parameters

Licensor:             Example Ltd

Licensed Work:        QueryGate 1.2.3

Change Date:          2030-07-18

Change License:       Apache License, Version 2.0
"""


def _stamped(**overrides) -> str:
    text = STAMPED
    for field, value in overrides.items():
        label = {
            "work": "Licensed Work:",
            "change_date": "Change Date:",
            "change_license": "Change License:",
        }[field]
        line = next(l for l in text.splitlines() if l.startswith(label))
        text = text.replace(line, f"{label}{' ' * (22 - len(label))}{value}")
    return text


# --- live guard ---------------------------------------------------------------


def test_live_license_passes_the_gate():
    problems = check_change_date.check()
    assert problems == [], "\n".join(problems)


def test_live_license_is_either_a_consistent_draft_or_a_correct_stamp():
    """Whichever state the repo is in, it must be internally consistent."""
    text = check_change_date.LICENSE_FILE.read_text()
    if check_change_date.is_draft(text):
        # A half-filled draft is how a placeholder ships unnoticed.
        assert check_change_date.LICENSOR_PLACEHOLDER in text
        assert check_change_date.check(release=True), (
            "an unstamped draft must fail the release-mode gate — otherwise the "
            "placeholder can reach a tagged release"
        )
    else:
        assert check_change_date.check(release=True) == []


def test_live_change_license_is_the_recorded_decision():
    text = check_change_date.LICENSE_FILE.read_text()
    assert check_change_date.EXPECTED_CHANGE_LICENSE in text


def test_live_changelog_release_heading_is_parseable():
    """The stamp is derived from this heading, so its format is load-bearing."""
    assert check_change_date.release_date("0.1.0") == date(2026, 7, 18)


def test_live_grant_matches_the_decided_shape_a_text():
    """The licence must carry the grant the owner decided, not a paraphrase."""
    license_text = " ".join(check_change_date.LICENSE_FILE.read_text().split())
    for fragment in (
        "You may make production use of the Licensed Work",
        "hosted, managed, or embedded basis, whether or not for a fee",
        "solely on behalf of, and for the internal use of, a single licensee",
        '"You" includes all entities that control, are controlled by, or are under '
        "common control with you",
    ):
        assert " ".join(fragment.split()) in license_text


# --- the arithmetic -----------------------------------------------------------


def test_change_date_is_exactly_four_years_after_release():
    assert check_change_date.CHANGE_DATE_YEARS == 4
    assert check_change_date.change_date_for(date(2026, 7, 18)) == date(2030, 7, 18)


def test_a_leap_day_release_does_not_crash_or_slip_a_year():
    """29 Feb + 4 years is 29 Feb again; the non-leap fallback is the guard for a
    different `CHANGE_DATE_YEARS`, and must land in the same year either way."""
    assert check_change_date.change_date_for(date(2028, 2, 29)) == date(2032, 2, 29)
    assert check_change_date.change_date_for(date(2028, 2, 29), years=1) == date(2029, 2, 28)


def test_an_undated_version_has_no_release_date():
    """`[Unreleased]` must not yield a date — an unreleased version cannot be stamped."""
    assert check_change_date.release_date("Unreleased") is None
    assert check_change_date.release_date("99.99.99") is None


# --- detector tests -----------------------------------------------------------


def test_a_correct_stamp_produces_no_problem():
    """Negative control: without it, a gate that fails everything would pass above."""
    assert (
        check_change_date.check(license_text=STAMPED, version="1.2.3", released=date(2026, 7, 18))
        == []
    )


def test_a_change_date_that_is_not_four_years_out_fails():
    problems = check_change_date.check(
        license_text=_stamped(change_date="2029-07-18"),
        version="1.2.3",
        released=date(2026, 7, 18),
    )
    assert len(problems) == 1
    assert "must be 2030-07-18" in problems[0]


def test_an_off_by_one_day_stamp_fails():
    """The realistic typo, and the one a human eye slides over."""
    problems = check_change_date.check(
        license_text=_stamped(change_date="2030-07-ights"),
        version="1.2.3",
        released=date(2026, 7, 18),
    )
    assert len(problems) == 1
    assert "not an ISO date" in problems[0]

    problems = check_change_date.check(
        license_text=_stamped(change_date="2030-07-19"),
        version="1.2.3",
        released=date(2026, 7, 18),
    )
    assert len(problems) == 1
    assert "2030-07-18" in problems[0]


def test_a_version_bump_without_re_stamping_fails():
    """The whole point of "per release": the previous release's date must not ride
    along on the next version."""
    problems = check_change_date.check(
        license_text=STAMPED, version="1.3.0", released=date(2026, 7, 18)
    )
    assert any("does not name the current version '1.3.0'" in p for p in problems)


def test_a_silently_weakened_change_license_fails():
    problems = check_change_date.check(
        license_text=_stamped(change_license="Proprietary"),
        version="1.2.3",
        released=date(2026, 7, 18),
    )
    assert any("Change License 'Proprietary'" in p for p in problems)


def test_a_stamped_version_with_no_changelog_entry_fails():
    problems = check_change_date.check(license_text=STAMPED, version="1.2.3", released=None)
    assert len(problems) == 1
    assert "no dated entry for version 1.2.3" in problems[0]


def test_an_unstamped_draft_passes_normally_but_fails_release_mode():
    draft = STAMPED.replace("2030-07-18", "<CHANGE_DATE — stamped per release>").replace(
        "Example Ltd", "<LICENSOR — legal entity, to be supplied>"
    )
    assert check_change_date.check(license_text=draft, version="1.2.3") == []
    problems = check_change_date.check(license_text=draft, version="1.2.3", release=True)
    assert any("must not ship it" in p for p in problems)


def test_a_half_filled_draft_fails_even_in_normal_mode():
    """Banner still present but the Licensor quietly filled in — the state in which
    a placeholder-free-looking draft gets mistaken for a finished licence."""
    draft = STAMPED.replace("2030-07-18", "<CHANGE_DATE — stamped per release>")
    problems = check_change_date.check(license_text=draft, version="1.2.3")
    assert len(problems) == 1
    assert "half-filled" in problems[0]


def test_release_mode_rejects_a_stamped_licence_that_kept_its_licensor_placeholder():
    text = _stamped().replace("Example Ltd", "<LICENSOR — legal entity, to be supplied>")
    problems = check_change_date.check(
        license_text=text, version="1.2.3", released=date(2026, 7, 18), release=True
    )
    assert any("Licensor placeholder" in p for p in problems)


def test_a_placeholder_that_wraps_onto_a_second_line_fails():
    """The defect a release rehearsal caught: a BSL parameter's value legitimately
    continues onto indented lines (the copyright sits under `Licensed Work:`), but
    the stamper replaces exactly one line — so a wrapped placeholder would leave
    its own tail orphaned inside the licence text."""
    wrapped = STAMPED.replace(
        "Change Date:          2030-07-18",
        "Change Date:          <CHANGE_DATE — stamped per release, see\n"
        "                      docs/RELEASING.md>",
    )
    problems = check_change_date.check(license_text=wrapped, version="1.2.3")
    assert any("wraps onto a second line" in p for p in problems)


def test_a_single_line_placeholder_is_accepted():
    """Negative control for the rule above."""
    fine = STAMPED.replace(
        "Change Date:          2030-07-18",
        "Change Date:          <CHANGE_DATE — stamped per release>",
    )
    problems = check_change_date.check(license_text=fine, version="1.2.3")
    assert not any("wraps" in p for p in problems)


def test_stamping_a_wrapped_placeholder_would_orphan_its_tail(tmp_path, monkeypatch):
    """Proves the failure the rule prevents is real, not theoretical."""
    license_file = tmp_path / "LICENSE"
    license_file.write_text(
        STAMPED.replace(
            "Change Date:          2030-07-18",
            "Change Date:          <CHANGE_DATE — stamped, see\n                      MORE.md>",
        )
    )
    monkeypatch.setattr(check_change_date, "LICENSE_FILE", license_file)
    monkeypatch.setattr(check_change_date, "project_version", lambda: "1.2.3")
    check_change_date.stamp(on=date(2026, 7, 18))
    assert (
        "MORE.md>" in license_file.read_text()
    ), "if this ever stops orphaning, the wrap rule can be relaxed"


# --- the process contract -----------------------------------------------------


def test_main_returns_nonzero_when_the_stamp_is_wrong(monkeypatch, capsys):
    monkeypatch.setattr(check_change_date, "check", lambda **kwargs: ["planted problem"])
    monkeypatch.setattr(sys, "argv", ["check_change_date.py", "--check"])
    assert check_change_date.main() == 1
    assert "planted problem" in capsys.readouterr().err


def test_main_release_flag_reaches_the_check(monkeypatch):
    seen = {}

    def fake_check(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_change_date, "check", fake_check)
    monkeypatch.setattr(sys, "argv", ["check_change_date.py", "--check", "--release"])
    assert check_change_date.main() == 0
    assert seen["release"] is True


def test_stamp_writes_the_derived_date(monkeypatch, tmp_path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text(
        STAMPED.replace("2030-07-18", "<CHANGE_DATE — x>").replace(
            "QueryGate 1.2.3", "QueryGate, all versions."
        )
    )
    monkeypatch.setattr(check_change_date, "LICENSE_FILE", license_file)
    monkeypatch.setattr(check_change_date, "project_version", lambda: "2.0.0")

    version, stamped = check_change_date.stamp(on=date(2027, 3, 1))

    assert (version, stamped) == ("2.0.0", date(2031, 3, 1))
    written = license_file.read_text()
    assert "Licensed Work:        QueryGate 2.0.0" in written
    assert "Change Date:          2031-03-01" in written
    # And the result must satisfy the gate it exists to satisfy.
    assert (
        check_change_date.check(license_text=written, version="2.0.0", released=date(2027, 3, 1))
        == []
    )


def test_stamp_refuses_a_version_with_no_release_date(monkeypatch, tmp_path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text(STAMPED)
    monkeypatch.setattr(check_change_date, "LICENSE_FILE", license_file)
    monkeypatch.setattr(check_change_date, "project_version", lambda: "9.9.9")
    monkeypatch.setattr(check_change_date, "release_date", lambda version, **kw: None)
    with pytest.raises(SystemExit, match="no dated entry"):
        check_change_date.stamp()
    assert license_file.read_text() == STAMPED, "a refused stamp must not write"
