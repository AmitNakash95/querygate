#!/usr/bin/env python3
"""EULA placeholder gate — `docs/legal/EULA.{en,he}.md`.

QueryGate ships proprietary under a commercial subscription (TODO.md item 210,
`docs/business/GTM_SAAS.md`), so `docs/legal/EULA.en.md` — not `LICENSE` — is
the licence of record, and it is the document a customer is asked to accept.

It currently carries unfilled placeholders (`[LICENSOR LEGAL NAME]`,
`[ADDRESS]`, `[EFFECTIVE DATE]`, …). Shipping a signed image whose licence of
record still says `[ADDRESS]` is the failure this gate exists to prevent, and
until now *nothing* referenced these files — no test, no script, no CI job. The
BSL Change-Date gate this replaces had exactly that shape (draft passes in
normal mode, refused in release mode); the shape is kept, the subject changes.

**The placeholder pattern is deliberately not `\\[[A-Z_ ]+\\]`.** `EULA.he.md`'s
placeholders are Hebrew — `[השם המשפטי של מעניק הרישיון]`, `[כתובת]`,
`[תאריך תחילה]` — so an ASCII-uppercase pattern gives a green build on an
entirely unfilled Hebrew licence of record. Any bracketed span counts, with
markdown links excluded.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EULA_FILES = ("docs/legal/EULA.en.md", "docs/legal/EULA.he.md")

#: `LICENSE` ships inside the wheel, the sdist and the image — enforced by
#: `scripts/check_release_artifacts.py`. It is therefore part of what a release
#: publishes, and it currently carries a draft banner saying it grants nothing.
#:
#: The deleted BSL Change-Date gate refused a release while that banner was
#: present. Nothing else did, so dropping it left a hole: a tag pushed today
#: would publish a signed image whose licence reads "NOT YET IN FORCE". Covered
#: here rather than left to be rediscovered.
LICENCE_FILE = "LICENSE"
_DRAFT_BANNER = "DRAFT — FOR LAWYER REVIEW"
#: `LICENSE` uses angle-bracket placeholders (`<LICENSOR — legal entity, to be
#: supplied>`), not the EULA's square brackets, so it needs its own pattern.
_ANGLE_SPAN_RE = re.compile(r"<([^<>\n]{2,120})>")


def _is_angle_placeholder(body: str) -> bool:
    """Whether a `<...>` span is a fill-in blank rather than prose.

    An ALL-CAPS-only pattern missed `<licensor legal entity, to be supplied>`;
    widening it to accept lowercase immediately started matching `<html>` and
    `<a@b.com>`. So shape matters more than case: a placeholder *describes* a
    blank, so it either shouts or carries a separator. An email, a URL and an
    HTML tag do none of that.
    """
    if any(ch in body for ch in "@/"):
        return False
    if body.isupper():
        return True
    return " " in body or "_" in body


# Any `[...]` span that is NOT a markdown link. Three exclusions, all needed:
#   (?<!\])  — the *label* half of a reference link, `[text][ref]`; without this
#              `[ref]` reads as a placeholder because nothing follows it.
#   (?![(\[]) — the *text* half of an inline `[text](url)` or reference link.
# Bounded length so a stray bracket does not swallow the rest of the line.
_PLACEHOLDER_RE = re.compile(r"(?<!\])\[([^\[\]\n]{2,120})\](?![(\[])")

# Spans that are legitimately bracketed prose rather than a fill-in blank.
_ALLOWED = frozenset({"sic", "BRACKETED"})

#: The deleted BSL gate pinned `Change License: Apache License, Version 2.0`
#: "so a silent edit cannot weaken it". That specific pin died with the flip,
#: but its PURPOSE — a positive assertion about what LICENSE says — had no
#: successor, so an empty or accidentally-permissive LICENSE passed cleanly.
#: These match operative GRANT clauses, not mentions: the BSL draft names
#: "Apache License, Version 2.0" as a Change License parameter and must not trip.
_PERMISSIVE_GRANTS = (
    ("MIT", re.compile(r"Permission is hereby granted, free of charge", re.I)),
    ("Apache-2.0", re.compile(r"Licensed under the Apache License", re.I)),
    ("BSD", re.compile(r"Redistribution and use in source and binary forms", re.I)),
    ("GPL", re.compile(r"GNU (GENERAL|LESSER GENERAL) PUBLIC LICENSE", re.I)),
)

#: A LICENSE shorter than this is not a licence.
_MIN_LICENCE_CHARS = 200


#: A placeholder whose closing delimiter is on the *next* line. Both patterns
#: above exclude `\n`, so a wrapped placeholder is invisible to them — a
#: false negative in the dangerous direction. The BSL gate this replaces had a
#: dedicated test for exactly this shape (`test_a_placeholder_that_wraps_onto_a
#: _second_line_fails`), so dropping the coverage would have been a regression.
#: Deliberately narrow: an opening delimiter followed by placeholder-shaped
#: content (uppercase ASCII, or any non-Latin script) and no close on the line.
_WRAPPED_RE = re.compile(
    r"(?:\[|<)(?:[A-Z][A-Za-z_ ]{2,}|[^\x00-\x7F][^\]\>\n]{2,})[^\]\>\n]*$",
    re.MULTILINE,
)


def wrapped_placeholders(text: str) -> list[str]:
    """Lines that open a placeholder and never close it on the same line."""
    return [m.group(0).strip() for m in _WRAPPED_RE.finditer(text)]


def placeholders(text: str) -> list[str]:
    """Every unfilled placeholder in `text`, in order of appearance.

    Includes wrapped ones (`_WRAPPED_RE`), reported by the line that opens them,
    so a placeholder cannot hide by straddling a line break.
    """
    found = [m.group(1) for m in _PLACEHOLDER_RE.finditer(text) if m.group(1) not in _ALLOWED]
    return found + wrapped_placeholders(text)


def check(*, release: bool, texts: dict[str, str] | None = None) -> list[str]:
    """Problems with the EULA files. Empty list means the gate passes.

    In normal mode an unfilled draft is reported but tolerated — the licence is
    still with counsel. In release mode any placeholder is fatal, because the
    artifact being signed carries the document.
    """
    problems: list[str] = []
    sources = texts if texts is not None else {p: (ROOT / p).read_text("utf-8") for p in EULA_FILES}

    for name, text in sorted(sources.items()):
        found = placeholders(text)
        if found and release:
            shown = ", ".join(repr(f) for f in found[:6])
            more = f" (+{len(found) - 6} more)" if len(found) > 6 else ""
            problems.append(
                f"{name} still carries {len(found)} unfilled placeholder(s) in release mode: "
                f"{shown}{more}. The licence of record cannot ship with blanks."
            )

    # `LICENSE` is checked against the real file only — `texts` is a seam for
    # the EULA detectors and must never substitute for it.
    if texts is None:
        problems.extend(
            check_licence_file((ROOT / LICENCE_FILE).read_text("utf-8"), release=release)
        )
    return problems


def check_licence_file(text: str, *, release: bool = True) -> list[str]:
    """Problems with `LICENSE`.

    Two tiers, deliberately. A permissive grant or a truncated file is **never**
    acceptable and is reported in both modes, so the per-push gate has real
    teeth. The draft banner and unfilled placeholders are a *known, tracked*
    state (item 210 replaces the BSL draft with the proprietary notice), so they
    are release-only — failing every push on already-scheduled work just teaches
    people to ignore a red CI.
    """
    problems: list[str] = []
    for name, pattern in _PERMISSIVE_GRANTS:
        if pattern.search(text):
            problems.append(
                f"{LICENCE_FILE} contains the operative grant clause of a permissive "
                f"licence ({name}). QueryGate ships proprietary; publishing an image "
                "whose LICENSE gives the product away is not recoverable."
            )
    if len(text.strip()) < _MIN_LICENCE_CHARS:
        problems.append(
            f"{LICENCE_FILE} is {len(text.strip())} characters — too short to be a "
            "licence. An empty or truncated LICENSE ships in the wheel, sdist and image."
        )
    if not release:
        return problems
    if _DRAFT_BANNER in text:
        problems.append(
            f"{LICENCE_FILE} still carries the draft banner ({_DRAFT_BANNER!r}), which says "
            "the file grants nothing and is not in force. It ships inside the wheel, the "
            "sdist and the image, so a release would publish a product with no licence."
        )
    angle = (
        [m.group(1) for m in _ANGLE_SPAN_RE.finditer(text) if _is_angle_placeholder(m.group(1))]
        + [m.group(1) for m in _PLACEHOLDER_RE.finditer(text) if m.group(1) not in _ALLOWED]
        + wrapped_placeholders(text)
    )
    if angle:
        shown = ", ".join(repr(a) for a in angle[:4])
        more = f" (+{len(angle) - 4} more)" if len(angle) > 4 else ""
        problems.append(
            f"{LICENCE_FILE} still carries {len(angle)} unfilled placeholder(s): {shown}{more}."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report problems (default)")
    parser.add_argument(
        "--release",
        action="store_true",
        help="refuse any unfilled placeholder — use before tagging a release",
    )
    args = parser.parse_args(argv)

    problems = check(release=args.release)
    if problems:
        print("EULA gate failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if not args.release:
        counts = {p: len(placeholders((ROOT / p).read_text("utf-8"))) for p in EULA_FILES}
        outstanding = sum(counts.values())
        if outstanding:
            print(
                f"EULA gate: OK for development — {outstanding} placeholder(s) still "
                f"outstanding across {len(EULA_FILES)} file(s): "
                + ", ".join(f"{p}={n}" for p, n in counts.items())
            )
            return 0
    print("EULA gate: OK — no unfilled placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
