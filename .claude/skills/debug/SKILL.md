---
name: debug
description: >-
  Diagnose a bug down to its actual root cause and fix it with a regression test
  that fails first. Use when something is broken, failing, flaky, throwing, or
  behaving unexpectedly, and when asked to investigate or fix a defect. Enforces
  reproduce-before-fix, root-cause-over-symptom, and revert-verify — pull the fix
  back out, watch the bug return, re-apply, watch it go — and records the failure
  so it is not rediscovered later.
---

# debug — reproduce, isolate, fix at the root

Bug: **$ARGUMENTS**

The failure mode this skill exists to prevent is the plausible fix: a change that
makes the symptom disappear without anyone establishing why it happened. It works,
it ships, and the real defect surfaces later somewhere less convenient.

**Do not edit production code until step 3.**

## 1. Check whether this is already known

Search `git log --oneline -30` and `git log --all --grep` for a prior fix in this
area, and check CLAUDE.md's "Testing gotchas" section — several past regressions
(the `CompensationStore` async-conversion gap, the item-100/114 unguarded
enforcement lines) are already documented there with what caused them. Also check
this session's own memory of prior guidance for this repo, if any applies.

This step costs a minute and regularly saves the whole investigation.

## 2. Reproduce it, reliably

Get a deterministic reproduction before theorizing. Write down:

- The exact command or input, the environment, and the **full** error output.
- What actually happens vs. what should happen — precisely, not "it breaks".
- How reliably it reproduces. **Intermittent is a fact about the bug, not an
  inconvenience** — it points at ordering, concurrency, time, or shared state,
  and that is a strong clue rather than an obstacle. `execution/concurrency.py`
  and the async-store conversion gotcha in CLAUDE.md are exactly this class.

If you cannot reproduce it, say so and stop rather than fixing by inference.
State what you would need — a log, a payload, the environment, a failing case.
Fixing a bug you have never seen fail means you cannot tell whether you fixed it.

## 3. Isolate before you theorize

Narrow the failing surface first:

- Bisect the input: what is the smallest input (AST, policy, connection) that
  still fails?
- Bisect the code path: the request pipeline is ordered (policy validation →
  schema validation → compiler → concurrency → engine → audit) — log or inspect
  at each boundary to find where the data is still right and where it is first
  wrong. **The bug is between those two points.**
- Bisect history if it used to work: `git log`/`git bisect` on the failing case.

Then form a hypothesis that **explains every observed detail**, including the
inconvenient ones — why it fails here and not there, why intermittently, why with
this input. A hypothesis that explains the failure but not the pattern around it
is not the root cause yet.

Test the hypothesis by prediction: it should let you predict a *new* case that
also fails. If the prediction misses, the hypothesis is wrong — go back rather
than patching it with an epicycle.

## 4. Find the root cause, not the nearest cause

Ask "why" until you reach something you can actually fix and defend. Ask two
more questions before fixing:

- **Where else does this root cause apply?** The same mistake is usually at
  sibling call sites — e.g. every `DialectAdapter`/`SessionDialectAdapter`
  concrete class, or every route/MCP tool wrapping the same service method.
  Grep for the pattern and check each.
- **What let this reach here?** A missing guard, an untested path, a wrong type,
  a silent `except`, a validation/schema check that runs in the wrong order.
  That is often the more valuable fix.

## 5. Fix it, test-first

1. **Write the failing test first** — a regression test for _this_ bug, named for
   the symptom, that stays in the suite permanently. Run it, and **watch it fail
   for the reason you diagnosed** — not merely fail. A test that fails for a
   different reason proves nothing and will pass after an unrelated change.
2. Make the smallest fix at the **root cause**. Resist the drive-by cleanup; it
   makes the fix unreviewable and the revert risky.
3. Run the test — it passes. Run the surrounding tests — still green.
4. Fix the sibling sites you found, each with its own test.
5. **Revert-verify the whole fix** (see below) — the step that proves your change
   is what fixed it.
6. Run the relevant tier (`poetry run pytest -m unit` at minimum; add
   integration/security/real-db per the surface you touched) and capture the
   real output. Run the wider regression suite when the fix touched shared code,
   a boundary, or a non-negotiable.

If the proper fix is large or touches a non-negotiable, **stop and surface it**
with a concrete proposal. If a stopgap is genuinely needed meanwhile, label it
explicitly, add it as the next `TODO.md` item, and never call it the fix.

## 6. Revert-verify — prove the fix is what fixed it

A green suite after a fix shows the symptom is gone. It does not show **your
change** is the reason. Rebuilds, restarts, cleared caches, a fixture edit, an
unrelated commit pulled in mid-session — any of these can carry the credit, and
the actual defect stays in the tree. CLAUDE.md's "Working agreement" already
mandates this for every bug fix; this is where it actually happens.

With the fix in and everything green, close the loop in both directions:

1. **Take the fix back out** — `git stash` (or `git stash push <paths>` to strip
   the fix while keeping the new test in place).
2. **Re-run the original reproduction from step 2 and the new test.** The bug
   must **reappear with the same symptom** and the test must fail for the
   diagnosed reason. If it does not come back, stop — you have not found the
   cause yet, and steps 3–4 need redoing.
3. **Put the fix back** — `git stash pop`. Confirm nothing was lost
   (`git diff`).
4. **Re-run both again.** Repro clean, test green.

Record the actual output of both directions. `Reverted, bug came back` with no
symptom named and no command shown is a claim, not evidence — and this repo does
not accept the claim.

Skip this only when reverting is genuinely impossible (the repro needs state the
revert destroys, a one-shot production incident) — and **say that you skipped it
and why**, in the report.

## 7. Record it so it is not rediscovered

The regression test *is* the primary record — it is permanent and it runs on
every future change. Beyond that:

- If the root cause is a durable trap specific to this repo's patterns (the
  shape of a future mistake, not just this one bug), add it to CLAUDE.md's
  "Testing gotchas" section so the next agent reads it before repeating it.
- If it reveals a decision that needs to be made rather than just a bug fixed
  (a genuine ambiguity in a non-negotiable, a missing invariant), record it as a
  `docs/PRODUCT_GUIDE.md` Decision Log entry or a new `TODO.md` item rather than
  papering over it in the fix.

## 8. Report

```
Symptom:      <what was reported>
Reproduced:   <the exact command/input, and how reliably>
Root cause:   <the real one, at file:line>
Not the cause:<the plausible explanations you ruled out, and how>
Fix:          <what changed, and why there>
Test:         <the regression test; confirm it failed first for the right reason>
Revert-verified: <bug reappeared: symptom> → <gone on re-apply>, via <command>
                 (or why the revert cycle was impossible)
Elsewhere:    <sibling sites found and fixed, or "none">
Regression:   <wider suite run + real result, or why the narrow tier sufficed>
Recorded:     <CLAUDE.md gotcha added, Decision Log entry, or TODO item — or "none needed">
Verified:     <commands run and their real results>
Not run:      <what you skipped and why>
```

Then close with the CLAUDE.md report format and the honest 1–10.
