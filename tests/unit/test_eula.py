"""EULA placeholder gate — `scripts/check_eula.py`.

Replaces the release-mode half of the deleted BSL Change-Date gate (TODO.md
item 210). QueryGate ships proprietary, so `docs/legal/EULA.{en,he}.md` is the
licence of record; before this, nothing in the repository referenced those files
at all, at the exact moment they became load-bearing.

Same shape as the guard it replaces and as `test_worklist_consistency.py`: a
live half that pins the real repository, plus detector tests that plant drift
and require each rule to fire. A gate whose detectors are missing is a gate
nobody can prove works.
"""

from __future__ import annotations

import pytest

from scripts import check_eula

pytestmark = pytest.mark.unit


# --- live guard ---------------------------------------------------------------


def test_live_eula_files_exist_and_are_readable():
    for name in check_eula.EULA_FILES:
        text = (check_eula.ROOT / name).read_text("utf-8")
        assert text.strip(), f"{name} is empty"


def test_live_normal_mode_tolerates_the_unfilled_draft():
    """Counsel has not settled the text yet; development must not be blocked."""
    assert check_eula.check(release=False) == []


def test_live_release_mode_currently_refuses():
    """Documents outstanding work rather than asserting a green that isn't real.

    When counsel returns the filled text this test flips to asserting `== []`.
    Until then it pins that the gate would *stop* a release, which is the whole
    reason it exists.
    """
    problems = check_eula.check(release=True)
    assert problems, (
        "the EULA now has no placeholders — fill in this test's expectation "
        "(assert problems == []) so the gate keeps asserting something"
    )


# --- detectors ----------------------------------------------------------------


def test_a_fully_filled_document_passes_release_mode():
    filled = "# EULA\n\nBetween Acme Ltd, of 1 Example Street, effective 2026-01-01.\n"
    assert check_eula.check(release=True, texts={"EULA.en.md": filled}) == []


def test_an_ascii_only_pattern_would_have_missed_the_hebrew_placeholders():
    """The regression this gate was written around.

    `EULA.he.md`'s placeholders are Hebrew, so the obvious `\\[[A-Z_ ]+\\]`
    pattern gives a green build on an entirely unfilled Hebrew licence of
    record. Assert the real pattern catches what that one would not.
    """
    hebrew_only = "בין [השם המשפטי של מעניק הרישיון], מ[כתובת], החל מ[תאריך תחילה].\n"
    found = check_eula.placeholders(hebrew_only)
    assert len(found) == 3, found
    assert check_eula.check(release=True, texts={"EULA.he.md": hebrew_only})


def test_markdown_links_are_not_placeholders():
    """Otherwise every cross-reference in the document reads as a blank."""
    text = "See [the policy guide](../POLICY.md) and [item 210][ref] for detail.\n"
    assert check_eula.placeholders(text) == []


def test_a_partially_filled_document_still_fails_release_mode():
    """The dangerous case: it looks finished at a glance."""
    half = "Between Acme Ltd, of [ADDRESS], effective 2026-01-01.\n"
    problems = check_eula.check(release=True, texts={"EULA.en.md": half})
    assert problems and "ADDRESS" in problems[0]


def test_normal_mode_never_fails_on_a_placeholder():
    """The mode split is the point; assert it rather than trusting it."""
    unfilled = "Between [LICENSOR LEGAL NAME], of [ADDRESS].\n"
    assert check_eula.check(release=False, texts={"EULA.en.md": unfilled}) == []


def test_the_problem_message_names_the_file_and_the_count():
    """A gate that fails without saying which document is a support ticket."""
    unfilled = "Between [LICENSOR LEGAL NAME], of [ADDRESS].\n"
    problems = check_eula.check(release=True, texts={"docs/legal/EULA.en.md": unfilled})
    assert len(problems) == 1
    assert "docs/legal/EULA.en.md" in problems[0]
    assert "2 unfilled placeholder" in problems[0]


# --- LICENSE, not just the EULA -----------------------------------------------
#
# The deleted BSL Change-Date gate refused a release while `LICENSE` carried its
# draft banner. Replacing it with an EULA-only gate left that ungated: `LICENSE`
# ships inside the wheel, sdist and image (enforced by
# `scripts/check_release_artifacts.py`), so a tag pushed today would publish a
# signed image whose licence says "NOT YET IN FORCE". These pin the recovered
# coverage.


def test_live_license_still_carries_the_draft_banner():
    """Documents the real state. Flips to the negative when the notice lands."""
    text = (check_eula.ROOT / check_eula.LICENCE_FILE).read_text("utf-8")
    assert check_eula.check_licence_file(text), (
        "LICENSE no longer looks like a draft — update this test and "
        "test_live_release_mode_currently_refuses together"
    )


def test_release_mode_refuses_the_draft_license_via_the_top_level_check():
    """`check(release=True)` must reach LICENSE, not only the EULA files."""
    problems = check_eula.check(release=True)
    assert any(check_eula.LICENCE_FILE in p for p in problems), problems


def test_a_settled_license_passes():
    settled = "Parameters\n\nLicensor: Acme Ltd\n\nLicensed Work: QueryGate 1.0.0\n"
    assert check_eula.check_licence_file(settled) == []


def test_the_draft_banner_alone_is_enough_to_refuse():
    """Placeholders could all be filled and the banner still make it inert."""
    banner_only = "DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.\n\nLicensor: Acme Ltd\n"
    problems = check_eula.check_licence_file(banner_only)
    assert len(problems) == 1 and "draft banner" in problems[0]


def test_angle_placeholders_alone_are_enough_to_refuse():
    """And the inverse: banner removed, placeholders forgotten."""
    text = "Licensor: <LICENSOR — legal entity, to be supplied>\n"
    problems = check_eula.check_licence_file(text)
    assert len(problems) == 1 and "placeholder" in problems[0]


def test_a_texts_seam_cannot_be_used_to_skip_the_license_check():
    """`texts` exists for the EULA detectors; it must not disable LICENSE."""
    assert check_eula.check(release=True, texts={}) == []
    assert any(
        check_eula.LICENCE_FILE in p for p in check_eula.check(release=True)
    ), "the real call must still reach LICENSE"


def test_prose_in_angle_brackets_is_not_a_placeholder():
    """`<html>` or an email in angle brackets must not read as a blank."""
    assert check_eula.check_licence_file("Contact <a@b.com> or see <html>.\n") == []
