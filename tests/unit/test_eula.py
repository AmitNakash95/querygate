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
    settled = (
        "QueryGate Commercial Licence\n\nLicensor: Acme Ltd\n\n"
        "Licensed Work: QueryGate 1.0.0\n\n"
        + "Proprietary and confidential. All rights reserved. " * 6
    )
    assert check_eula.check_licence_file(settled) == []


def test_the_draft_banner_alone_is_enough_to_refuse():
    """Placeholders could all be filled and the banner still make it inert."""
    banner_only = (
        "DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.\n\nLicensor: Acme Ltd\n"
        + "QueryGate is licensed, not sold. All rights reserved. " * 6
    )
    problems = check_eula.check_licence_file(banner_only)
    assert len(problems) == 1 and "draft banner" in problems[0]


def test_angle_placeholders_alone_are_enough_to_refuse():
    """And the inverse: banner removed, placeholders forgotten."""
    text = (
        "Licensor: <LICENSOR — legal entity, to be supplied>\n"
        + "QueryGate is licensed, not sold. All rights reserved. " * 6
    )
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
    prose = (
        "Contact <a@b.com> or see <html>.\n"
        + "QueryGate is licensed, not sold. All rights reserved. " * 6
    )
    assert check_eula.check_licence_file(prose) == []


# --- wrapped placeholders -----------------------------------------------------
#
# Both delimiter patterns exclude `\n`, so a placeholder split across two lines
# was invisible to them. The BSL gate this replaces had a dedicated test for that
# shape; dropping the coverage would have been a silent regression.


def test_a_placeholder_wrapping_onto_a_second_line_is_caught():
    wrapped = "Licensor:  <LICENSOR — legal entity,\n           to be supplied>\n"
    assert check_eula.wrapped_placeholders(wrapped)
    assert check_eula.check_licence_file(wrapped)


def test_a_wrapped_square_bracket_placeholder_is_caught():
    wrapped = "Between [LICENSOR LEGAL\nNAME], of somewhere.\n"
    assert check_eula.placeholders(wrapped)
    assert check_eula.check(release=True, texts={"docs/legal/EULA.en.md": wrapped})


def test_a_wrapped_hebrew_placeholder_is_caught():
    """The Hebrew file is the one an ASCII-shaped guard misses twice over."""
    wrapped = "בין [השם המשפטי\nשל מעניק הרישיון], מכתובת.\n"
    assert check_eula.wrapped_placeholders(wrapped)


def test_ordinary_prose_with_a_less_than_sign_is_not_a_wrapped_placeholder():
    for benign in (
        "The term is less than 12 months, and\nrenews automatically.\n",
        "See section 4 (a < b) for the\ncalculation.\n",
        "Contact us at <support@example.com>\nfor questions.\n",
        "A [markdown link](../x.md) and a\nsecond line.\n",
    ):
        assert check_eula.wrapped_placeholders(benign) == [], benign


def test_a_settled_licence_has_no_wrapped_placeholders():
    settled = "Parameters\n\nLicensor: Acme Ltd\n\nLicensed Work: QueryGate 1.0.0\n"
    assert check_eula.wrapped_placeholders(settled) == []


# --- the CLI contract ---------------------------------------------------------
#
# `main()` is the ONLY thing the Makefile and both CI workflows ever call, and it
# had no test. The deleted `test_change_date.py` had two for exactly this layer.
# Without them, mutating `check(release=args.release)` to `check(release=False)`
# leaves every other test green and turns the pre-tag release gate into a
# permanent no-op — a tag would publish a signed image with a blank licence.


def test_main_returns_nonzero_when_the_gate_finds_problems(monkeypatch, capsys):
    monkeypatch.setattr(check_eula, "check", lambda **kw: ["planted problem"])
    assert check_eula.main(["--check"]) == 1
    assert "planted problem" in capsys.readouterr().err


def test_main_returns_zero_when_the_gate_is_clean(monkeypatch):
    monkeypatch.setattr(check_eula, "check", lambda **kw: [])
    assert check_eula.main(["--check", "--release"]) == 0


def test_the_release_flag_actually_reaches_the_check(monkeypatch):
    """The mutation that would silently disable the release gate."""
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_eula, "check", spy)
    check_eula.main(["--check", "--release"])
    assert seen.get("release") is True, seen


def test_a_bare_check_does_not_silently_enable_release_mode(monkeypatch):
    """The negative half — otherwise development is blocked by unsettled text."""
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_eula, "check", spy)
    check_eula.main(["--check"])
    assert seen.get("release") is False, seen


def test_main_passes_no_texts_seam(monkeypatch):
    """`texts` is test-only. If production ever passed it, the LICENSE half of
    the gate would silently vanish and nothing else would notice."""
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_eula, "check", spy)
    check_eula.main(["--check", "--release"])
    assert "texts" not in seen, seen


# --- narrowing the live tests to their stated subjects -------------------------
#
# `test_live_release_mode_currently_refuses` asserted only that SOME problem
# exists — and LICENSE supplies two before the EULA is consulted. So it would
# have stayed green after counsel filled the EULA, never prompting the flip its
# own failure message asks for. Same shape for the draft-banner test.


def test_live_release_mode_refuses_specifically_because_of_the_eula():
    problems = check_eula.check(release=True)
    assert any(
        name in p for p in problems for name in check_eula.EULA_FILES
    ), f"no EULA-attributed problem; the gate is only firing on LICENSE: {problems}"


def test_live_license_specifically_carries_the_draft_banner():
    text = (check_eula.ROOT / check_eula.LICENCE_FILE).read_text("utf-8")
    problems = check_eula.check_licence_file(text)
    assert any("draft banner" in p for p in problems), problems


# --- the _ALLOWED exclusions --------------------------------------------------


def test_the_allowed_exclusions_actually_suppress():
    assert check_eula.placeholders("Replace every [BRACKETED] placeholder\n") == []


def test_the_allowed_exclusions_do_not_swallow_real_blanks():
    """Guards against someone 'fixing' a noisy gate by widening _ALLOWED."""
    assert "ADDRESS" in check_eula.placeholders("of [ADDRESS].\n")
    assert "LICENSOR LEGAL NAME" in check_eula.placeholders("[LICENSOR LEGAL NAME] of\n")


# --- the LICENSE content rules ------------------------------------------------
#
# These had no tests when first written — I verified them interactively and a
# `git checkout` silently reverted the lot with the suite still green, which is
# precisely the "guard that cannot fail" pattern. Pinned now.
#
# The deleted BSL gate pinned `Change License: Apache License, Version 2.0`
# "so a silent edit cannot weaken it". Its purpose — a POSITIVE assertion about
# what LICENSE says — is what these restore.

_PAD = "Proprietary and confidential. All rights reserved. " * 6


def test_a_permissive_grant_in_license_is_refused():
    """The unrecoverable case: shipping a proprietary product under MIT."""
    for text, expected in (
        ("MIT License\n\nPermission is hereby granted, free of charge, to any person", "MIT"),
        ("Licensed under the Apache License, Version 2.0 (the 'License')", "Apache-2.0"),
        ("Redistribution and use in source and binary forms, with or without", "BSD"),
        ("GNU GENERAL PUBLIC LICENSE\nVersion 3", "GPL"),
    ):
        problems = check_eula.check_licence_file(text + _PAD)
        assert any(expected in p for p in problems), f"{expected} not refused: {problems}"


def test_the_bsl_change_license_parameter_is_not_a_grant():
    """A mention must not trip the gate — the BSL draft names Apache-2.0 as its
    Change License, and that is a parameter, not a grant."""
    text = "Change License:       Apache License, Version 2.0\n" + _PAD
    assert not any("Apache" in p for p in check_eula.check_licence_file(text))


def test_an_empty_or_truncated_license_is_refused():
    for text in ("", "   \n", "All rights reserved."):
        problems = check_eula.check_licence_file(text)
        assert any("too short" in p for p in problems), f"{text!r} accepted: {problems}"


def test_the_content_rules_fire_in_dev_mode_too():
    """Two tiers: a permissive grant is never acceptable, so unlike the draft
    banner it must not wait for release mode."""
    mit = "Permission is hereby granted, free of charge, to any person" + _PAD
    assert check_eula.check_licence_file(mit, release=False)
    banner = "DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.\n" + _PAD
    assert check_eula.check_licence_file(banner, release=False) == []


def test_square_bracket_placeholders_in_license_are_checked():
    """LICENSE previously used only the angle pattern, so the EULA's bracket
    style — which docs/RELEASING.md advertises as gated — went unchecked."""
    text = "Licensor: [LICENSOR LEGAL NAME]\n" + _PAD
    assert check_eula.check_licence_file(text)


def test_a_lowercase_angle_placeholder_is_caught():
    text = "Licensor: <licensor legal entity, to be supplied>\n" + _PAD
    assert check_eula.check_licence_file(text)


def test_html_tags_and_emails_are_not_placeholders():
    """The false-positive control for widening past ALL-CAPS."""
    for body in ("html", "br", "a@b.com", "support@example.com", "https://x.y"):
        assert not check_eula._is_angle_placeholder(body), body
    for body in ("LICENSOR LEGAL NAME", "licensor legal entity", "LICENSOR_NAME", "XX"):
        assert check_eula._is_angle_placeholder(body), body
