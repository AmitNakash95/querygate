---
name: roadmap-next
description: >-
  Continue QueryGate development in ROADMAP.md order — automatically pick up
  where the last agent left off and implement the next roadmap item. Use when
  asked to "continue the roadmap", "pick up where we left off", "work the
  roadmap", "next roadmap item", or to resume roadmap-driven development. Reads
  order from ROADMAP.md and done-status from TODO.md (the authority), so it is
  stateless and cannot drift. Optionally pass an item number to force selection.
---

# roadmap-next — resume roadmap-driven development

Selects the next item by **ROADMAP.md order** (not ascending TODO number, which
is what the `next-item` skill uses), then executes it with the full `next-item`
implementation discipline. "Where the last agent left off" is **derived**, never
stored: walk ROADMAP.md order and find the first item not yet `✅ DONE` in
TODO.md. There is no separate progress file to go stale.

`args` may be a TODO item number to force selection (it must still pass the
skip/dependency/capacity rules below); blank = auto-select.

## 1. Orient

- Read `ROADMAP.md`, `TODO.md`, `CLAUDE.md`, `README.md`,
  `docs/THREAT_MODEL.md`, `docs/RELEASING.md`.
- `git status` + recent commits; confirm the worktree is clean before starting.
- **Never** reset, amend, delete, or recreate the local `v0.1.0` tag.
- Don't modify completed (`✅ DONE`) items unless fixing a *verified* regression.

## 2. Locate where the last agent left off (the selection algorithm)

TODO.md is the authority for done-status; ROADMAP.md is the authority for order.

1. Walk `ROADMAP.md`'s ordered roadmap **top-to-bottom**, phase by phase.
2. For each item, look up its `###` heading in `TODO.md`. It is **complete**
   iff that heading ends in exactly `✅ DONE` (no trailing qualifier like
   `(phase 1)` / `(phase 2 not started)`). A phased item with open phases is
   **not** complete.
3. If complete: reconcile ROADMAP.md's checkbox for it to `[x]` (fix silently
   if wrong — TODO.md leads) and continue.
4. The **next item** is the first one that is *all* of:
   - not fully `✅ DONE`,
   - **not** in ROADMAP.md's "Decision-gated — NOT in the automated order"
     list, and
   - has every TODO.md "Depends on" dependency satisfied (spot-check against
     code/tests; don't trust a stale `✅` blindly).
5. If a decision-gated item is reached before the next eligible one, **skip and
   report it** — never auto-start a decision-gated item (governed writes,
   NL→StructuredQuery, open AST standard). Same for the write-up test in the
   `next-item` skill: skip anything whose body says it needs a maintainer
   product/design/UX/infra decision.
6. For **coordination-gated** items (53 audit, 60 bug bounty, 54 mapping): do
   only the code/doc-preparable parts and clearly flag what needs a
   human/vendor to finish. Don't claim completion of the external part.
7. If `args` forces an item number, honor it, but still apply the skip and
   dependency rules and say so.

Then **announce**: the last completed roadmap item (where the previous agent
left off), the next item and why it's next per the roadmap rationale, and any
items skipped and why.

## 3. State remaining session capacity

State the remaining session limit (time/tokens/usage) before committing to the
item. If the platform doesn't expose an exact number, say so — don't invent one
— and estimate conservatively. Capacity is a **selection constraint**: reserve
room to inspect existing code, implement, add tests, update docs, run release
checks, review the diff, and commit. If the next roadmap item can't fully fit
(impl + tests + docs + checks + review + commit) and can't be cleanly phased,
say so and stop — do **not** silently skip ahead to a smaller, out-of-order
item. Report the constraint and let the human decide.

## 4. Implement (delegate to the next-item discipline)

Follow the `next-item` skill's steps 4–6 exactly for the selected item:

- **Scope & implement** (its §4): state scope, acceptance criteria (the item's
  "why"), and likely files after inspecting existing abstractions/tests.
  Production-grade, deny-by-default where security-sensitive. Preserve the core
  invariant: **no caller-controlled raw SQL reaches the database.** Follow the
  composable-interfaces doctrine; use `dialect-primitive` for dialect work.
  Add unit/integration/security/real-db coverage as appropriate. Update
  README/docs/TODO to match shipped behavior; consider `product-guide-sync`.
- **Phasing** (its §5): if too large, split into explicit phases *in TODO.md*
  and ship the first independently-useful slice only.
- **Gate & commit** (its §6): run `make release-check` (and `make
  release-smoke` if the change touches packaging/containers/config
  defaults/DB execution/deployment — see `release-gate`). Use `ship-item` to
  stub+archive the item once every phase is `✅ DONE`. Commit as **one**
  informative commit. Do **not** push or move tags without approval.

## 5. Close the loop on the roadmap

In the **same commit** as the item's work:

- Tick the item's checkbox to `[x]` in `ROADMAP.md` **only if** its TODO.md
  heading now ends in `✅ DONE` (a phased item that only advanced one phase
  stays `[ ]`).
- If, while working, you concluded the roadmap order should change (a
  dependency surfaced, a pilot shifted priorities), don't silently reorder —
  make the edit a deliberate one-line-reasoned change per ROADMAP.md's
  "Maintenance rules" and call it out in your report.

## 6. Report

State: where the last agent left off, what you selected and why, what you
skipped and why, what shipped (impl + tests + docs + gates), the commit, and
what the **next** roadmap item will be for the following agent.
