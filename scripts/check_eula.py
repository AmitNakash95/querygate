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

# Any `[...]` span that is NOT a markdown link. Three exclusions, all needed:
#   (?<!\])  — the *label* half of a reference link, `[text][ref]`; without this
#              `[ref]` reads as a placeholder because nothing follows it.
#   (?![(\[]) — the *text* half of an inline `[text](url)` or reference link.
# Bounded length so a stray bracket does not swallow the rest of the line.
_PLACEHOLDER_RE = re.compile(r"(?<!\])\[([^\[\]\n]{2,120})\](?![(\[])")

# Spans that are legitimately bracketed prose rather than a fill-in blank.
_ALLOWED = frozenset({"sic", "BRACKETED"})


def placeholders(text: str) -> list[str]:
    """Every unfilled placeholder in `text`, in order of appearance."""
    return [m.group(1) for m in _PLACEHOLDER_RE.finditer(text) if m.group(1) not in _ALLOWED]


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
