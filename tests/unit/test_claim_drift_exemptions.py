"""The claim-drift scanner's whole-file exemptions must stay earned.

`scripts/claim_drift_sites.py` is a finding aid, not a gate, and its `KNOWN_OK`
list is how a previous sweep records "I read this hit and it is correct." Most
entries are a single line. Item 210 added something sharper: **seven
whole-document exemptions**, each justified by the document carrying a
SUPERSEDED or RETIRED banner rather than by anything about the individual lines.

That justification is exactly the kind that rots silently. Someone tidying a
banner away — or copying a live claim into one of these files because "it is
already exempt" — would leave the exemption in place with nothing behind it, and
the licensing sweep would report clean over roughly 150 false claims about a
cancelled Business Source Licence flip. Nothing else in the repository would
notice, because the scanner is deliberately not a gate.

So the banner is asserted here instead: a whole-file exemption is valid only
while the file says, at the top, that it is superseded.
"""

from __future__ import annotations

import pytest

import re

from scripts import claim_drift_sites

pytestmark = pytest.mark.unit

#: How far into the file the banner must appear. A superseded notice below this
#: is not a banner — it is a footnote a reader reaches after the obsolete
#: content has already been believed. Measured: the seven live banners sit at
#: offsets 32-62, so 300 is ~5x head-room. The first draft used 1200, which is
#: 20x and would have accepted a notice buried well below the fold.
_BANNER_WINDOW_CHARS = 300

#: Whole-file exemptions that are NOT banner-backed, listed so a twentieth one
#: cannot be added without a deliberate edit here. These are the worklist files,
#: the gate scripts and their tests, and this scanner itself — surfaces that
#: DESCRIBE drift (a docstring explaining which claim a guard retired) rather
#: than making it. Every one is scoped to a single claim class in `KNOWN_OK`.
_NON_BANNER_WHOLE_FILE = frozenset(
    {
        "ROADMAP.md",
        "TODO.md",
        "docs/TODO_ARCHIVE.md",
        "scripts/check_licenses.py",
        "scripts/check_release_artifacts.py",
        "scripts/claim_drift_sites.py",
        "tests/integration/test_mssql_cost_estimation.py",
        "tests/security/test_no_phone_home.py",
        "tests/unit/test_claim_drift_exemptions.py",
        "tests/unit/test_release_metadata.py",
        "tests/unit/test_third_party_licenses.py",
    }
)


def _whole_file_exemptions() -> set[str]:
    return {path for path, needle, _, _ in claim_drift_sites.KNOWN_OK if needle == ""}


def test_every_banner_exempt_document_exists_and_is_whole_file_exempt():
    """Forward direction: a banner requirement must protect a real exemption."""
    whole_file = _whole_file_exemptions()
    for name in claim_drift_sites.BANNER_EXEMPT:
        assert (claim_drift_sites.REPO / name).is_file(), f"{name} no longer exists"
        assert name in whole_file, f"{name} requires a banner but has no whole-file exemption"


def test_no_whole_file_exemption_exists_without_a_banner_or_an_explicit_waiver():
    """Reverse direction — the half that was missing.

    The forward check alone let a new whole-file entry be added to `KNOWN_OK`
    with no banner requirement and nothing anywhere failing. A whole-file
    exemption hides every future claim in that file, so adding one must cost a
    deliberate edit in two places.
    """
    unaccounted = (
        _whole_file_exemptions() - set(claim_drift_sites.BANNER_EXEMPT) - _NON_BANNER_WHOLE_FILE
    )
    assert not unaccounted, (
        "these files are exempt from a claim sweep in their ENTIRETY with neither a "
        "superseded banner nor an entry in _NON_BANNER_WHOLE_FILE. Add the banner, or "
        f"record here why the file cannot make a claim: {sorted(unaccounted)}"
    )
    # And the removal direction: a waiver left behind after its exemption is
    # deleted silently pre-authorises re-adding it later.
    stale = _NON_BANNER_WHOLE_FILE - _whole_file_exemptions()
    assert not stale, (
        "_NON_BANNER_WHOLE_FILE waives files that are no longer whole-file exempt; "
        f"delete the waiver rather than leaving it to pre-authorise a future one: {sorted(stale)}"
    )


def test_every_exemption_names_a_real_claim_class():
    """`\"*\"` is the blunt instrument; everything else must resolve."""
    for path, _, _, classes in claim_drift_sites.KNOWN_OK:
        if classes == "*":
            continue
        for name in (c.strip() for c in classes.split(",")):
            assert (
                name in claim_drift_sites._CLASSES
            ), f"{path} is exempt from unknown class {name!r}"


def test_every_banner_exempt_document_still_carries_its_banner():
    missing = []
    for name, marker in claim_drift_sites.BANNER_EXEMPT.items():
        head = (claim_drift_sites.REPO / name).read_text("utf-8")[:_BANNER_WINDOW_CHARS]
        # Anchored to a blockquote line, not `marker in head`. `.github/cla/README.md`
        # carries "RETIRED" in its own H1, so the substring form stayed green with
        # the entire banner deleted — one of the seven was unguarded.
        if not re.search(rf"^>.*{re.escape(marker)}", head, re.M):
            missing.append(
                f"{name} (expected {marker!r} in its first {_BANNER_WINDOW_CHARS} chars)"
            )
    assert not missing, (
        "these documents are exempted from the claim-drift sweep in their ENTIRETY, on the "
        "strength of a banner declaring them obsolete. The banner is gone, so the exemption "
        "is now hiding live claims:\n  " + "\n  ".join(missing)
    )


def test_the_licensing_class_still_matches_the_claims_it_was_written_for():
    """Negative control. The exemptions above are only safe while the patterns
    they exempt *from* still fire — an exemption over a broken detector reads
    identical to a clean sweep."""
    for line in (
        "QueryGate will be licensed under the Business Source License 1.1",
        "internal production use is free forever, with no user cap",
        "it makes no outbound calls to us, ever",
        "there is no kill switch, no time bomb",
        "each version converts to Apache-2.0 after four years",
        "QueryGate is source-available, not open source",
    ):
        assert claim_drift_sites._CLASSES["licensing"].matches(line), line
    for benign in (
        "QueryGate is proprietary, closed-source, and sold as a paid subscription.",
        "The entitlement refresh transmits four fields and no database content.",
    ):
        assert not claim_drift_sites._CLASSES["licensing"].matches(benign), benign
