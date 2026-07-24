#!/usr/bin/env python3
"""Worklist consistency guard — TODO.md / ROADMAP.md / docs/TODO_ARCHIVE.md.

CLAUDE.md's worklist rules (item numbering, archive-on-ship, stub-leave-behind,
ROADMAP reconciliation) used to live only as prose, so they drifted silently —
fully-shipped items were left full-bodied in TODO.md instead of archived, the
Quick-scan table's ✅ column fell out of sync with the headings, and the archive
went out of numeric order. This script turns those prose rules into a mechanical
gate, mirroring how ``scripts/`` already guards other invariants (``generate_sbom``,
``check_release``) and how ``querygate-scope-catalog`` + ``test_scope_catalog``
guard the generated scope doc.

Single source of truth for "is item N done": its ``### N.`` heading in **TODO.md**.
  * *done* (fully shipped)  -> the heading text ends in exactly ``✅ DONE``.
  * *resolved* (has ✅ mark) -> the heading text contains ``✅`` at all
    (covers ``✅ DONE (phase 1)``, ``✅ OBSOLETE (...)`` — started but not fully done).

Invariants checked (all mechanical; a violation exits non-zero):

  A. No duplicate ``### N.`` item headings in TODO.md.
  B. Archive headings are unique and strictly ascending by number.
  C. Every *done* item is archived, its TODO body carries the
     ``(item N)`` write-up pointer, and that body is a stub (not a full
     un-migrated write-up).
  D. Every archived item's TODO heading is *done* (reverse of C).
  E. Quick-scan table ✅ column mirrors each heading's *resolved* state, and
     the table and headings cover exactly the same item numbers.
  F. Every bare ``- [ ]/[x] **N**`` ROADMAP checkbox mirrors *done*.
     (Compound bullets like ``**30 + 89 (phase 2)**`` are hand-curated and
     intentionally skipped.)
  G. Every ``(item N)`` write-up pointer in TODO.md resolves to an archived item.

``--fix`` regenerates the two *purely derived* mirrors — the table ✅ column (E)
and the bare ROADMAP checkboxes (F) — from the headings, so reconciling them is
one command instead of hand edits. It never touches archival, stubbing, or
numbering (C/D/B/G): those need a human judgement (the one-line stub summary,
where the body moves) and stay check-only. The ★ flagship tag is a hand-kept
category marker, not derived, so ``--fix`` preserves it.

Exit 0 = clean (or ``--fix`` applied cleanly), 1 = violations remain.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TODO = ROOT / "TODO.md"
ROADMAP = ROOT / "ROADMAP.md"
ARCHIVE = ROOT / "docs" / "TODO_ARCHIVE.md"

_HEAD_RE = re.compile(r"^### (\d+)\. (.*)$")
_TABLE_RE = re.compile(r"^\| (\d+) \| (.*?) \| ")
_ROAD_BOX_RE = re.compile(r"^(- \[)([ x])(\] \*\*)(\d+)(\*\*)")
# Only the archive *write-up pointer* ("… TODO_ARCHIVE.md) (item N)"), never a
# bare prose cross-reference like "(item 19)" — those legitimately name any item.
_POINTER_RE = re.compile(r"TODO_ARCHIVE\.md\)\s*\(item (\d+)\)")

# A *done* item's body should be a one-line-summary stub. Real stubs top out
# around 15 lines; a full un-migrated write-up runs 40–400. Well clear of both.
_STUB_MAX_LINES = 40


def _done(title: str) -> bool:
    """Fully shipped: heading ends in exactly ``✅ DONE`` (no trailing qualifier)."""
    return title.rstrip().endswith("✅ DONE")


def _resolved(title: str) -> bool:
    """Has any ✅ terminal mark (done, a shipped phase, or obsolete)."""
    return "✅" in title


def _parse_todo(text: str):
    """Return (headings, bodies, table) for TODO.md.

    headings: list of (n, title) in file order.
    bodies:   {n: [lines]} from the ``### N.`` line up to the next ``##``/``###``.
    table:    list of (n, cell) Quick-scan rows.
    """
    lines = text.split("\n")
    headings, bodies = [], {}
    bound_idx = [i for i, l in enumerate(lines) if re.match(r"^#{2,3} ", l)]
    head_lines = {i: _HEAD_RE.match(lines[i]) for i in bound_idx if _HEAD_RE.match(lines[i])}
    for pos, i in enumerate(bound_idx):
        m = head_lines.get(i)
        if not m:
            continue
        n = int(m.group(1))
        headings.append((n, m.group(2)))
        end = bound_idx[pos + 1] if pos + 1 < len(bound_idx) else len(lines)
        bodies[n] = lines[i:end]
    table = [(int(m.group(1)), m.group(2)) for m in (_TABLE_RE.match(l) for l in lines) if m]
    return headings, bodies, table


def _archive_numbers(text: str):
    return [int(m.group(1)) for m in re.finditer(r"^### (\d+)\.", text, re.M)]


def find_violations(
    todo: str | None = None, road: str | None = None, arch: str | None = None
) -> list[str]:
    """Return a list of human-readable violation strings ([] means clean).

    The three docs default to the live repo files; pass text explicitly to
    exercise the detector on synthetic inputs (see the unit tests).
    """
    todo = TODO.read_text(encoding="utf-8") if todo is None else todo
    road = ROADMAP.read_text(encoding="utf-8") if road is None else road
    arch = ARCHIVE.read_text(encoding="utf-8") if arch is None else arch

    headings, bodies, table = _parse_todo(todo)
    title = {n: t for n, t in headings}
    arch_nums = _archive_numbers(arch)
    arch_set = set(arch_nums)
    v: list[str] = []

    # A — no duplicate item numbers in TODO.
    seen: set[int] = set()
    for n, _ in headings:
        if n in seen:
            v.append(f"A: item {n} has a duplicate `### {n}.` heading in TODO.md")
        seen.add(n)

    # B — archive unique + strictly ascending.
    if len(arch_nums) != len(arch_set):
        dups = sorted({n for n in arch_nums if arch_nums.count(n) > 1})
        v.append(f"B: duplicate `### N.` heading(s) in TODO_ARCHIVE.md: {dups}")
    if arch_nums != sorted(arch_nums):
        v.append("B: TODO_ARCHIVE.md items are not in ascending numeric order")

    # C — done items are archived, stubbed, and carry a pointer.
    for n, t in headings:
        if not _done(t):
            continue
        if n not in arch_set:
            v.append(f"C: item {n} is `✅ DONE` but has no `### {n}.` in TODO_ARCHIVE.md")
        body = "\n".join(bodies[n])
        if not re.search(rf"TODO_ARCHIVE\.md\)\s*\(item {n}\)", body):
            v.append(
                f"C: item {n} is `✅ DONE` but its TODO body has no TODO_ARCHIVE `(item {n})` write-up pointer"
            )
        if len(bodies[n]) > _STUB_MAX_LINES:
            v.append(
                f"C: item {n} is `✅ DONE` but its TODO body is {len(bodies[n])} lines "
                f"(> {_STUB_MAX_LINES}) — move the write-up to TODO_ARCHIVE.md and leave a stub"
            )

    # D — every archived item's heading is done (reverse of C).
    for n in arch_nums:
        if n not in title:
            v.append(f"D: item {n} is in TODO_ARCHIVE.md but has no `### {n}.` heading in TODO.md")
        elif not _done(title[n]):
            v.append(f"D: item {n} is archived but its TODO heading is not `✅ DONE`")

    # E — Quick-scan table mirrors *resolved*, and covers the same numbers.
    table_nums = {n for n, _ in table}
    head_nums = {n for n, _ in headings}
    for n in sorted(head_nums - table_nums):
        v.append(f"E: item {n} has a heading but no Quick-scan table row")
    for n in sorted(table_nums - head_nums):
        v.append(f"E: item {n} is in the Quick-scan table but has no `### {n}.` heading")
    for n, cell in table:
        if n not in title:
            continue
        if ("✅" in cell) != _resolved(title[n]):
            v.append(
                f"E: item {n} Quick-scan ✅ mark disagrees with its heading "
                f"(table {'has' if '✅' in cell else 'lacks'} ✅; heading "
                f"{'has' if _resolved(title[n]) else 'lacks'} ✅)"
            )

    # F — bare **N** ROADMAP checkboxes mirror *done*.
    for line in road.split("\n"):
        m = _ROAD_BOX_RE.match(line)
        if not m:
            continue
        n, box = int(m.group(4)), m.group(2) == "x"
        if n in title and box != _done(title[n]):
            v.append(
                f"F: item {n} ROADMAP checkbox is [{'x' if box else ' '}] but its heading "
                f"is {'`✅ DONE`' if _done(title[n]) else 'not fully done'}"
            )

    # G — no dangling write-up pointers.
    for n, lines in bodies.items():
        for m in _POINTER_RE.finditer("\n".join(lines)):
            target = int(m.group(1))
            if target not in arch_set:
                v.append(
                    f"G: item {n}'s body points at `(item {target})` but that item is not archived"
                )

    return v


def apply_fix() -> bool:
    """Regenerate the derived table ✅ column and bare ROADMAP checkboxes. Returns changed."""
    todo = TODO.read_text(encoding="utf-8")
    headings, _, _ = _parse_todo(todo)
    title = {n: t for n, t in headings}
    changed = False

    # Quick-scan table ✅ column — mirror *resolved*, preserving the ★ flagship tag.
    out_lines = []
    for line in todo.split("\n"):
        m = _TABLE_RE.match(line)
        if m and int(m.group(1)) in title:
            n = int(m.group(1))
            parts = line.split(" | ")  # ['| N', 'CELL', 'EFFORT', 'DEPS |']
            cell = parts[1]
            done_marker, star, core = False, False, cell
            while True:
                if core.startswith("✅ "):
                    done_marker, core = True, core[len("✅ ") :]
                    continue
                if core.startswith("★ "):
                    star, core = True, core[len("★ ") :]
                    continue
                break
            want = ("✅ " if _resolved(title[n]) else "") + ("★ " if star else "") + core
            if want != cell:
                parts[1] = want
                line = " | ".join(parts)
                changed = True
        out_lines.append(line)
    if changed:
        TODO.write_text("\n".join(out_lines), encoding="utf-8")

    # ROADMAP bare **N** checkboxes — mirror *done*.
    road = ROADMAP.read_text(encoding="utf-8")
    road_out, road_changed = [], False
    for line in road.split("\n"):
        m = _ROAD_BOX_RE.match(line)
        if m and int(m.group(4)) in title:
            want = "x" if _done(title[int(m.group(4))]) else " "
            if m.group(2) != want:
                line = m.group(1) + want + m.group(3) + m.group(4) + m.group(5) + line[m.end() :]
                road_changed = True
        road_out.append(line)
    if road_changed:
        ROADMAP.write_text("\n".join(road_out), encoding="utf-8")

    return changed or road_changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--fix",
        action="store_true",
        help="regenerate the derived table ✅ column and bare ROADMAP checkboxes from the headings",
    )
    args = ap.parse_args(argv)

    if args.fix:
        changed = apply_fix()
        print(
            "worklist mirrors regenerated (table ✅ + ROADMAP checkboxes)."
            if changed
            else "worklist mirrors already in sync — nothing to fix."
        )

    violations = find_violations()
    if violations:
        print(
            f"\nworklist consistency: {len(violations)} violation(s) "
            "in TODO.md / ROADMAP.md / docs/TODO_ARCHIVE.md:\n",
            file=sys.stderr,
        )
        for msg in violations:
            print(f"  - {msg}", file=sys.stderr)
        fixable = any(m[0] in "EF" for m in violations)
        print(
            "\nStructural violations (A/B/C/D/G) need a manual fix — archive the item, "
            "leave a stub, or renumber.\n"
            + (
                "Table/ROADMAP mirror drift (E/F) is auto-fixable: run "
                "`make worklist-sync` (or `python3 scripts/check_worklist.py --fix`).\n"
                if fixable
                else ""
            ),
            file=sys.stderr,
        )
        return 1

    print("worklist consistency: OK — TODO.md, ROADMAP.md, and TODO_ARCHIVE.md all reconcile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
