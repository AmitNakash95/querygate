# Agent instructions

Read and follow [`CLAUDE.md`](CLAUDE.md) as this repository's complete operating
contract. It applies to every coding agent, regardless of product or vendor.

One process rule is especially easy to miss, because it comes after the tests
are already green: **every top-level task ends by running the `auditors`
skill and triaging what it reports — before the self-review and before the
commit.** A passing suite is what those reviewers assume, not what they check.
See CLAUDE.md's "Working agreement" section for the mandatory completion gate.

Then close the task with the honest 1-10 self-review defined in the same
section, and reconcile `TODO.md` / `ROADMAP.md` and, if the change warrants
it, `docs/PRODUCT_GUIDE.md` (the `product-guide-sync` skill runs that check).

This file is a stable pointer. `CLAUDE.md` is the source of truth; do not
duplicate its content here.
