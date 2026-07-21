---
name: ship-item
description: >-
  Archive a fully-shipped TODO item correctly. Use when a TODO.md item's `###`
  heading ends in exactly `✅ DONE` (no trailing qualifier like `(phase 1)`) and
  you need to move its write-up to docs/TODO_ARCHIVE.md, leave the right stub in
  TODO.md, and keep item numbering intact. Triggers: "archive this item", "mark
  item N done", "this item shipped", after finishing an item that completes every
  phase.
---

# ship-item — archive a completed TODO item

Encodes the TODO/archive discipline from CLAUDE.md. Getting this wrong corrupts
durable artifacts (~176 permanent "item N" cross-refs, the archive ordering, the
quick-scan index), so follow it mechanically.

## Preconditions — verify before doing anything

1. The item's `###` heading in `TODO.md` ends in **exactly** `✅ DONE` with no
   trailing qualifier. If it reads `✅ DONE (phase 1)`, "phase 2 not started",
   or any partial marker, **STOP** — partially-done items keep their full body
   inline in `TODO.md` and are NOT archived. Only stub once every phase is done.
2. The work is actually complete and tested (don't archive on optimism).

## Steps

1. **Read** the item's full body in `TODO.md` and skim `docs/TODO_ARCHIVE.md` to
   find the correct numeric insertion point.
2. **Move the full body** into `docs/TODO_ARCHIVE.md`, inserting it in **numeric
   order** under a `### N. <same heading text> ✅ DONE` heading. The archive keeps
   the full "Shipped / Coverage / Decision Log / Why it matters" write-up. Never
   append out of order; never put an *open* action item in the archive.
3. **Replace the body in `TODO.md` with a stub** — the exact shape used by every
   other stub:
   - The same `### N. <heading> ✅ DONE` heading.
   - One line summarizing what shipped.
   - Then, on its own: `**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item N).`
4. **Update the Quick-scan summary table** in `TODO.md`: flip the item's row to
   `✅`. (The table indexes items 1–66; if the item is >66 and table-less, that's
   fine — the stub/heading is the authoritative anchor.)
5. **Verify cross-refs still resolve**: `grep -rn "item N" .` should still make
   sense — the number must NOT change.

## Hard rules (from CLAUDE.md)

- **Item numbers are permanent and file-global.** Never renumber or reuse. A new
  item takes the next unused number.
- Never create a second TODO file or a competing index.
- Never move an open/partial item to the archive.

## Verify

```bash
grep -n "### N\." TODO.md docs/TODO_ARCHIVE.md   # stub in TODO, full body in archive
grep -n "Full write-up.*(item N)" TODO.md         # stub pointer present
```
