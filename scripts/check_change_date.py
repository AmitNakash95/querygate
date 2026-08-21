#!/usr/bin/env python3
"""Stamp and gate the BSL Change Date in ``LICENSE``.

The Business Source License converts to its Change License on the Change Date.
QueryGate's decision (``docs/business/GTM_EXECUTION_PLAN.md`` §2.3) is **4 years
per release**, which means the date is not a constant written once — each
release stamps its own, and a release that ships the previous release's date has
silently given away four years of grant or taken them back.

**Why the Licensed Work names a version.** "4 years per release" and
``Licensed Work: QueryGate, all versions`` cannot both be true: one Change Date
cannot govern releases stamped in different years. Adopters that stamp per
release name the release in this parameter (CockroachDB 22.2, MariaDB MaxScale
24.08), and each released artifact carries its own ``LICENSE``. So the stamper writes
``QueryGate <version>``, and the gate fails if that version drifts from
``pyproject.toml``. This resolves a contradiction between
``PRE_BSL_CLEANUP_PLAN.md`` Phase 0 ("all versions") and the per-release
decision; the plans have been corrected to match.

**The stamp is derived, never typed.** The Change Date is computed as the
release date recorded in ``CHANGELOG.md`` for the current version, plus exactly
four years. Nothing here trusts a human to have done that arithmetic — the gate
recomputes it. A typo'd year is the whole failure mode this exists to catch.

Two modes, because the licence is a draft today and must not have a vacuous gate
in the meantime:

* ``--check`` (per commit, and in ``make release-check``) — if ``LICENSE`` is
  still the unstamped draft, assert it is *consistently* a draft and say so.
  Once stamped, every invariant below is enforced.
* ``--check --release`` (before tagging) — additionally requires that the draft
  banner and every placeholder are gone. This is the gate that stops a release
  going out with ``<LICENSOR>`` in it.

Usage::

    python3 scripts/check_change_date.py --check              # CI / release-check
    python3 scripts/check_change_date.py --check --release    # pre-tag
    python3 scripts/check_change_date.py --stamp              # cut a release
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE_FILE = ROOT / "LICENSE"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
PYPROJECT_FILE = ROOT / "pyproject.toml"

# The owner's decision, GTM_EXECUTION_PLAN.md §2.3. Changing this changes the
# licence for every subsequent release, so it is a named constant rather than a
# literal buried in the arithmetic.
CHANGE_DATE_YEARS = 4

# The Change License the Change Date converts to. BSL 1.1 requires a
# GPL-compatible licence here; pinned so a silent edit cannot weaken it.
EXPECTED_CHANGE_LICENSE = "Apache License, Version 2.0"

DRAFT_BANNER = "DRAFT — FOR LAWYER REVIEW"
LICENSOR_PLACEHOLDER = "<LICENSOR"
CHANGE_DATE_PLACEHOLDER = "<CHANGE_DATE"

_LICENSED_WORK_RE = re.compile(r"^Licensed Work:\s+(?P<value>.+?)$", re.M)
_CHANGE_DATE_RE = re.compile(r"^Change Date:\s+(?P<value>.+?)$", re.M)
_CHANGE_LICENSE_RE = re.compile(r"^Change License:\s+(?P<value>.+?)$", re.M)
# `## [0.1.0] — 2026-07-18`, with either an em dash or a hyphen as separator.
_RELEASE_HEADING_RE = re.compile(
    r"^##\s*\[(?P<version>[^\]]+)\]\s*[—-]\s*(?P<released>\d{4}-\d{2}-\d{2})\s*$", re.M
)


def project_version(pyproject: Path = PYPROJECT_FILE) -> str:
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def release_date(version: str, changelog: Path = CHANGELOG_FILE) -> date | None:
    """The date CHANGELOG.md records for `version`, or None if it has none.

    `[Unreleased]` deliberately has no date and therefore no release date — a
    version that has not been released cannot have a Change Date derived for it.
    """
    for match in _RELEASE_HEADING_RE.finditer(changelog.read_text()):
        if match.group("version") == version:
            return date.fromisoformat(match.group("released"))
    return None


def change_date_for(released: date, years: int = CHANGE_DATE_YEARS) -> date:
    """`released` plus `years`, handling 29 February without a leap-day crash."""
    try:
        return released.replace(year=released.year + years)
    except ValueError:  # 29 Feb -> 28 Feb in a non-leap year
        return released.replace(year=released.year + years, day=28)


def _field(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value").strip() if match else None


def is_draft(text: str) -> bool:
    return DRAFT_BANNER in text or CHANGE_DATE_PLACEHOLDER in text


def check(
    release: bool = False,
    license_text: str | None = None,
    version: str | None = None,
    released: date | None = None,
) -> list[str]:
    """Return every problem with the current stamp. Empty means the gate passes."""
    text = license_text if license_text is not None else LICENSE_FILE.read_text()
    version = version if version is not None else project_version()
    problems: list[str] = []

    change_license = _field(_CHANGE_LICENSE_RE, text)
    if change_license != EXPECTED_CHANGE_LICENSE:
        problems.append(
            f"LICENSE declares Change License {change_license!r}, expected "
            f"{EXPECTED_CHANGE_LICENSE!r} — the conversion licence is a recorded owner "
            f"decision, not an editable detail"
        )

    draft = is_draft(text)
    if draft:
        if release:
            problems.append(
                "LICENSE is still the unstamped draft (banner and/or placeholders present) "
                "— a release must not ship it. Fill the Licensor, then run "
                "`make stamp-change-date`."
            )
        # A parameter's value continues onto indented lines (the BSL template
        # puts the copyright statement under `Licensed Work:`), but the stamper
        # replaces exactly one line. A placeholder that wraps would therefore
        # leave its own tail orphaned inside the licence text — caught here
        # rather than discovered in a released legal document.
        for label, pattern in (
            ("Licensed Work", _LICENSED_WORK_RE),
            ("Change Date", _CHANGE_DATE_RE),
        ):
            value = _field(pattern, text) or ""
            if "<" in value and not value.rstrip().endswith(">"):
                problems.append(
                    f"LICENSE's {label} placeholder wraps onto a second line "
                    f"({value!r}). Stamping replaces one line, so the remainder would be "
                    f"orphaned into the licence — keep the placeholder on one line."
                )
        if LICENSOR_PLACEHOLDER not in text:
            problems.append(
                "LICENSE carries the draft banner but no Licensor placeholder — the draft "
                "is half-filled, which is how a placeholder ships unnoticed. Finish the "
                "stamp or restore the placeholder."
            )
        # Nothing further is checkable: an unstamped draft has no date to verify.
        return problems

    if release and LICENSOR_PLACEHOLDER in text:
        problems.append(
            "LICENSE still contains a Licensor placeholder — a release must not ship it"
        )

    licensed_work = _field(_LICENSED_WORK_RE, text) or ""
    if version not in licensed_work:
        problems.append(
            f"LICENSE's Licensed Work is {licensed_work!r}, which does not name the current "
            f"version {version!r} — the Change Date is stamped per release, so a version "
            f"bump requires re-stamping (`make stamp-change-date`)"
        )

    raw_date = _field(_CHANGE_DATE_RE, text)
    try:
        stamped = date.fromisoformat((raw_date or "").strip())
    except ValueError:
        problems.append(
            f"LICENSE's Change Date {raw_date!r} is not an ISO date (YYYY-MM-DD) — it must "
            f"be a single concrete date a reader can rely on"
        )
        return problems

    released = released if released is not None else release_date(version)
    if released is None:
        problems.append(
            f"CHANGELOG.md has no dated entry for version {version}, so the Change Date "
            f"cannot be derived. Add `## [{version}] — YYYY-MM-DD` before stamping"
        )
        return problems

    expected = change_date_for(released)
    if stamped != expected:
        problems.append(
            f"LICENSE's Change Date is {stamped.isoformat()}, but version {version} was "
            f"released {released.isoformat()}, so it must be "
            f"{expected.isoformat()} ({CHANGE_DATE_YEARS} years later). Re-stamp with "
            f"`make stamp-change-date`"
        )
    return problems


def stamp(on: date | None = None) -> tuple[str, date]:
    """Write the current version and its derived Change Date into LICENSE."""
    version = project_version()
    released = on if on is not None else release_date(version)
    if released is None:
        raise SystemExit(
            f"CHANGELOG.md has no dated entry for version {version}. Add "
            f"`## [{version}] — YYYY-MM-DD` first — the Change Date is derived from the "
            f"release date, never typed by hand."
        )
    text = LICENSE_FILE.read_text()
    new_date = change_date_for(released)

    if not _LICENSED_WORK_RE.search(text) or not _CHANGE_DATE_RE.search(text):
        raise SystemExit("LICENSE has no `Licensed Work:` / `Change Date:` parameter to stamp")

    text = _LICENSED_WORK_RE.sub(f"Licensed Work:        QueryGate {version}", text, count=1)
    text = _CHANGE_DATE_RE.sub(f"Change Date:          {new_date.isoformat()}", text, count=1)
    LICENSE_FILE.write_text(text)
    return version, new_date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify the stamp (default)")
    group.add_argument("--stamp", action="store_true", help="write the derived Change Date")
    parser.add_argument(
        "--release",
        action="store_true",
        help="with --check, also require that no draft banner or placeholder remains",
    )
    args = parser.parse_args()

    if args.stamp:
        version, new_date = stamp()
        print(f"stamped LICENSE: QueryGate {version}, Change Date {new_date.isoformat()}")
        return 0

    problems = check(release=args.release)
    if problems:
        print("Change Date gate FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    text = LICENSE_FILE.read_text()
    if is_draft(text):
        print(
            "Change Date gate OK: LICENSE is a consistent unstamped DRAFT "
            "(no release may ship it — `--check --release` refuses)"
        )
    else:
        print(f"Change Date gate OK: {_field(_CHANGE_DATE_RE, text)} for {project_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
