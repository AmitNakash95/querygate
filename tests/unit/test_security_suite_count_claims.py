"""The adversarial-suite size quoted in the docs must be the real one.

This number has drifted repeatedly. It sat at "260 cases" in three documents
while `SECURITY_POSTURE.md` said 480; then adding identifier-quoting tests made
480 stale everywhere; then the phone-home guard moved it again. It is quoted on
a public trust page, in the security-questionnaire answer, in the flagship
essay, and in a comparison page — exactly the places a claim being wrong is
expensive.

Hand-correcting it is not a fix, because nothing makes the next person do it. So
the count is asserted here: adding or removing a `security`-marked test fails
this test until the documents are updated, which is the same posture
`docs/THIRD_PARTY_LICENSES.md` takes for the dependency inventory.

Deliberately counted the way the docs describe it — `pytest -m security`, i.e.
what `make test-security` runs. Counting `tests/security/` instead under-reports
it, because some `security`-marked tests live beside the integration and unit
suites; that discrepancy is what made 464 look plausible.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Every surface that quotes the number. A document added later that quotes a
# count and is not listed here is not covered — that is a known bound of this
# guard, not an oversight; add it here when you add the claim.
CLAIM_FILES = (
    "docs/PRODUCT_GUIDE.md",
    "docs/SECURITY_POSTURE.md",
    "docs/TRUST_EVIDENCE.md",
    "docs/product-guide.html",
    "landing/security.html",
    "docs/business/COMPARISON_MICROSOFT_DAB.md",
    "docs/business/COMPARISON_GOOGLE_MCP_TOOLBOX.md",
    "docs/business/ESSAY_1_STRING_LAYER_CANNOT_WORK.md",
)

# `522 tests`, `522-test`, `522 adversarial tests`, `(522 cases)`.
_COUNT_RE = re.compile(r"\b(\d{2,5})[\s-](?:\w+\s)?(?:tests?|cases)\b|\b(\d{2,5})-test\b")
_CONTEXT_RE = re.compile(r"adversarial|bypass class|-m security|test-security", re.I)

# Counts that belong to a *different* suite and must not be dragged in.
_OTHER_SUITE_RE = re.compile(r"\bunit suite\b|\bfull (?:pre-existing )?unit\b", re.I)

# How far a context word may sit from the number and still be about it. Matching
# per *line* was wrong in both directions. `docs/product-guide.html` renders a
# paragraph per line, so a true claim about the **unit** suite ("2012 tests")
# shared a line with the word "adversarial" and the guard demanded it be changed
# to the security count — telling the reader to corrupt a correct statement. And
# in `docs/PRODUCT_GUIDE.md` a real claim wrapped so its context word fell on the
# next line, and went unchecked. A character window over normalised text fixes
# both directions.
_CONTEXT_WINDOW = 120


def collected_security_tests() -> int:
    """What `make test-security` would run, collected without executing it."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "security",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        # A CI runner's PYTEST_ADDOPTS is inherited by the child and would change
        # selection or output format, turning a claim-drift failure into a
        # mystery. `-o addopts=` clears the ini setting but not the env var.
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert result.returncode == 0, (
        "collection failed, so any count derived from it is meaningless:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    assert ids, f"collection produced no tests:\n{result.stdout}\n{result.stderr}"
    return len(ids)


def _quoted_counts(path: Path) -> set[int]:
    """Every suite-size number quoted near a word that is about *this* suite."""
    text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
    found: set[int] = set()
    for match in _COUNT_RE.finditer(text):
        value = match.group(1) or match.group(2)
        if not value or not (100 <= int(value) <= 9999):
            continue  # years, ports, byte sizes
        window = text[max(0, match.start() - _CONTEXT_WINDOW) : match.end() + _CONTEXT_WINDOW]
        if _OTHER_SUITE_RE.search(window):
            continue
        if _CONTEXT_RE.search(window):
            found.add(int(value))
    return found


def test_every_document_quotes_the_real_adversarial_suite_size():
    actual = collected_security_tests()
    wrong: list[str] = []
    for name in CLAIM_FILES:
        path = ROOT / name
        assert path.is_file(), f"{name} is in CLAIM_FILES but no longer exists — update the list"
        for quoted in _quoted_counts(path):
            if quoted != actual:
                wrong.append(f"{name} quotes {quoted}")
    assert not wrong, (
        f"`pytest -m security` collects {actual} tests, but: "
        + "; ".join(sorted(wrong))
        + f". Update the number to {actual} (and regenerate docs/TRUST_EVIDENCE.md with "
        f"`make trust-page` and docs/product-guide.html with `make product-guide-html`) — "
        f"a published count that the repo contradicts is a claim defect, not a typo."
    )


def test_the_count_is_measured_by_marker_not_by_directory():
    """`tests/security/` under-reports the suite; the docs describe the marker.

    Pinned because 464-vs-480 was exactly this confusion, and it looked like a
    contradiction in the repo rather than two different questions.
    """
    by_marker = collected_security_tests()
    by_directory = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/security",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert by_directory.returncode == 0, by_directory.stderr
    directory_ids = [line for line in by_directory.stdout.splitlines() if "::" in line]
    assert (
        directory_ids
    ), "collecting tests/security/ produced nothing — the comparison below would be vacuous"
    # The delta is the security-marked tests living beside the integration and
    # unit suites. Pinned as a floor rather than `> 0` so the assertion cannot be
    # satisfied by a single stray test.
    assert by_marker - len(directory_ids) >= 10, (
        f"{by_marker - len(directory_ids)} security-marked tests live outside "
        f"tests/security/; if that is no longer true, this test and the docstrings "
        f"citing it can be simplified"
    )


def test_a_true_claim_about_a_different_suite_is_not_dragged_in(tmp_path):
    """The defect this guard shipped with: `docs/product-guide.html` states the
    unit suite's size on the same rendered line as the word "adversarial", and the
    guard demanded that correct number be changed to the security count."""
    doc = tmp_path / "d.md"
    doc.write_text(
        "the adversarial suite grew, with the full pre-existing unit suite "
        "(2012 tests) passing unchanged after the sweep"
    )
    assert _quoted_counts(doc) == set()


def test_a_claim_whose_context_word_wraps_is_still_found(tmp_path):
    """The other direction: a real claim split across two lines must not escape."""
    doc = tmp_path / "d.md"
    doc.write_text("> OpenAPI fuzzing, and a 522-test\n> adversarial suite) on every change\n")
    assert _quoted_counts(doc) == {522}
