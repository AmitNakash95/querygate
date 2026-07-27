---
name: next-item
description: >-
  Drive the continue-development workflow: pick the next eligible TODO item,
  scope it to remaining session capacity, implement it production-grade,
  test/document it, run release gates, and commit as one clean commit. Use when
  asked to "continue development", "pick the next item", "work the TODO", or run
  prompts/continue_dev_prompt. Optionally pass an item number to override
  auto-selection.
---

# next-item — continue QueryGate development

The invokable form of [prompts/continue_dev_prompt](../../../prompts/continue_dev_prompt).
`args` may be a TODO item number to force selection; blank = auto-select.

## 1. Orient

- Read `README.md`, `TODO.md`, `CLAUDE.md`, `docs/THREAT_MODEL.md`,
  `docs/RELEASING.md`, and ROADMAP.md's active `🚧 **CLAIMED**` markers.
- `git status` + recent commits; confirm the worktree is clean.
- **Never** reset, amend, delete, or recreate the local `v0.1.0` tag.
- Don't modify completed (✅) items unless fixing a *verified* regression.

## 2. State remaining session capacity

State the remaining session limit (time/tokens/usage) before choosing. If the
platform doesn't expose an exact number, say so — don't invent one — and give a
conservative estimate. Capacity is a **selection constraint**: reserve room to
inspect existing code, implement, add tests, update docs, run release checks,
review the diff, and commit. Don't pick an item just because there's room to
write its implementation.

## 3. Select the item

- If `args` gives an item number, use it (capacity rule + phasing still apply).
- Otherwise scan the Quick-scan table for non-✅ items in ascending number order.
  An item is eligible only when every id in its "Depends on" column is ✅ (or "—").
- Treat an item carrying a ROADMAP.md `🚧 **CLAIMED**` marker as unavailable,
  including when `args` names it. Report the owner; never steal or auto-expire
  another agent's claim. Continue only to an independently eligible item.
- **Skip** any eligible item whose write-up says it needs a product/design/UX/
  infra decision from a maintainer ("which X to expose", "needs a chosen
  framework", "needs an approval model decided"). Note it, move on.
- Weigh scope vs. capacity. Select the first eligible, non-skipped item that
  fully fits (impl + tests + docs + checks + review + commit).
- Don't trust the ✅ marker blindly — spot-check deps against code/tests if
  anything looks stale.
- State: what you selected, what you skipped and why, how it fits capacity, and
  that deps are genuinely done.
- Before implementation, add the canonical ROADMAP.md claim immediately below
  the selected item's checkbox:
  ``🚧 **CLAIMED** — owner: `<agent/session-or-task-id>`; started:
  `<YYYY-MM-DDTHH:MMZ>` ``. Re-read the entry and proceed only if exactly one
  claim exists and it is yours. Do not make a standalone claim-only commit.

## 4. Scope & implement

- State proposed scope, acceptance criteria (the item's "why"), and files likely
  to change, after inspecting existing abstractions and tests.
- Production-grade, deny-by-default where security-sensitive. Preserve the core
  invariant: **no caller-controlled raw SQL reaches the database.**
- Follow the composable-interfaces doctrine (Protocol/registry, not scattered
  `if X ==`). See the `dialect-primitive` skill for dialect work.
- Add unit / integration / security / real-db coverage as appropriate.
- Update README/docs/TODO to match shipped behavior. Consider the
  `product-guide-sync` skill for PRODUCT_GUIDE.

## 5. Phasing (if the item is too large)

- First grep for an existing foundation to build on, not rebuild.
- Split into explicit phases **in TODO.md** (see items 26, 30 for the convention):
  ship one concrete, independently-useful slice now; record the rest as an
  explicit not-yet-started follow-up phase with a concrete reason for the split.
- Implement/test only the first phase, fully. If no shippable phase fits
  capacity, don't start — report the constraint and pick the next item.

## 6. Gate & commit

- Run `make release-check`. If the change touches packaging, containers, config
  defaults, DB execution, or deployment, also run `make release-smoke`.
  (See the `release-gate` skill.)
- Re-check capacity before committing; if tight, stop expanding and finish
  tests/docs/checks/review/commit. Don't claim completion with any check unfinished.
- Once every phase of the item is done, use the `ship-item` skill to stub+archive it.
- Remove your ROADMAP.md claim in the item's final commit, or before stopping if
  you hand the item back unfinished.
- Commit as **one** informative commit for this item — don't mix in unrelated or
  pre-existing changes. Do **not** push, publish, or move tags without approval.
