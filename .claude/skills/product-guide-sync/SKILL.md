---
name: product-guide-sync
description: >-
  Run the docs/PRODUCT_GUIDE.md maintenance protocol after a non-trivial change:
  decide whether the change added/altered architecture, a deliberate tradeoff, a
  new term/tool, or a customer-facing capability, and if so update the right
  section (and the Decision Log). Use after finishing a feature or design change,
  or when asked to "update the product guide" / "sync the docs". Skip for pure
  bug fixes, no-behavior refactors, and test-only changes.
---

# product-guide-sync — keep PRODUCT_GUIDE.md living

Enforces the maintenance protocol at the top of
[docs/PRODUCT_GUIDE.md](../../../docs/PRODUCT_GUIDE.md). This doc is the
plain-language, human-facing explainer (also used for marketing/positioning),
and it must stay in sync with the product.

## The decision (ask this first)

*Did the just-finished task introduce or change something a human would need to
understand the product's architecture, a technical decision, a new term/tool, or
a customer-facing capability?*

- **No** — pure bug fix, refactor with no behavioral/architectural change, or
  test-only change → **skip**. Don't pad the doc. Say you checked and it wasn't
  needed.
- **Yes** → make a small, targeted update (next section).

## If yes

1. Find the relevant section among the doc's ToC (What is QueryGate / Core
   Request Pipeline / Security Model / Connections, Policy & Configuration /
   Catalog & Semantic Layer / Auth & Transports / Testing, Release & Operations /
   Glossary / FAQ / Decision Log).
2. Add or update that section **in the same style** as the surrounding prose.
   Keep it small and accurate.
3. If the change was a **deliberate tradeoff** (chose X over Y for a reason),
   add an entry to the [Decision Log](../../../docs/PRODUCT_GUIDE.md#decision-log)
   — this is where "why, not just what" lives (e.g. item 74's reject-don't-emulate
   MSSQL nulls decision).
4. New term or tool? Add it to the Glossary.

## Hard rules

- **No large speculative rewrites** as a side effect of an unrelated task.
  Small, incremental, accurate edits only.
- Match the doc's existing voice: plain-language, human/marketing-facing — not
  internal engineering shorthand.
- If you added a Decision Log entry for a settled design posture, that posture is
  now precedent — reference it, don't re-litigate it later.
