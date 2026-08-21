"""The adversarial-suite size quoted in the docs must be the real one.

This number has drifted twice. It sat at "260 cases" in three documents while
`SECURITY_POSTURE.md` said 480; and the moment 38 identifier-quoting tests were
added it went stale again everywhere. It is quoted on a public trust page, in
the security-questionnaire answer, and in a comparison page — exactly the places
a claim being wrong is expensive.

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
    "landing/security.html",
)

# `518 tests`, `518-test`, `(518 tests)`, `✅ 518 tests`.
_COUNT_RE = re.compile(r"\b(\d{2,5})[\s-]tests?\b|\b(\d{2,5})[\s-]?(?:-test|cases)\b")
_CONTEXT_RE = re.compile(r"adversarial|bypass class|-m security|test-security", re.I)


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
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    assert ids, f"collection produced no tests:\n{result.stdout}\n{result.stderr}"
    return len(ids)


def _quoted_counts(path: Path) -> set[int]:
    """Every suite-size number quoted on a line that is talking about the suite."""
    found: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not _CONTEXT_RE.search(line):
            continue
        for match in _COUNT_RE.finditer(line):
            value = match.group(1) or match.group(2)
            # Ignore obvious non-counts (years, ports, byte sizes).
            if value and 100 <= int(value) <= 9999:
                found.add(int(value))
    return found


def test_every_document_quotes_the_real_adversarial_suite_size():
    actual = collected_security_tests()
    wrong: list[str] = []
    for name in CLAIM_FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        for quoted in _quoted_counts(path):
            if quoted != actual:
                wrong.append(f"{name} quotes {quoted}")
    assert not wrong, (
        f"`pytest -m security` collects {actual} tests, but: "
        + "; ".join(sorted(wrong))
        + f". Update the number to {actual} (and regenerate docs/TRUST_EVIDENCE.md with "
        f"`make trust-page`) — a published count that the repo contradicts is a claim "
        f"defect, not a typo."
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
    )
    directory_ids = [line for line in by_directory.stdout.splitlines() if "::" in line]
    assert by_marker > len(directory_ids), (
        "some `security`-marked tests are expected to live outside tests/security/; "
        "if that is no longer true, this test and the docstrings citing it can be simplified"
    )
