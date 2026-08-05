---
name: refactor
description: >-
  Restructure code without changing behavior — with the tests that pin the
  current behavior established first, and behavior change kept strictly out of
  the same commit. Use when asked to refactor, clean up, extract, rename,
  consolidate duplication, or migrate to a pattern. Enforces characterize-first
  and no-behavior-change-smuggled-in.
---

# refactor — change the structure, not the behavior

Refactor: **$ARGUMENTS**

The defining constraint: **observable behavior before and after is identical.**
The moment a refactor also fixes a bug, adds a check, or "improves" an edge case,
it stops being a refactor and becomes an unreviewable change — because nobody can
tell which diff hunks were supposed to be inert.

If you find a bug mid-refactor: **write it down, finish or stop the refactor, and
fix it separately.** Two commits, in that order. This is the single rule this
skill exists to enforce.

## 1. Justify it, and bound it

State the specific problem being solved: duplication that has already caused a
divergence, a variant chain that will be missed on the next addition (see
CLAUDE.md's "Composable single-purpose interfaces"), a module doing two jobs, a
boundary violation against the request pipeline. "Cleaner" is not a reason —
code churn has real cost in review, in merge conflicts, and in git blame.

Then bound it: what is in scope, and what you will leave alone even though you
will be tempted. Write that second list down; it is the one that protects the
diff.

Check CLAUDE.md's "Architecture" section (and `docs/PRODUCT_GUIDE.md` for the
plain-language version) for the pattern you are moving *toward*. A refactor
toward a shape the repo does not use, or that touches a non-negotiable, needs a
written decision in `docs/PRODUCT_GUIDE.md`'s Decision Log first — not a large
diff that presents the decision as a fait accompli.

## 2. Characterize the current behavior first

**Before touching anything**, establish the safety net.

1. Run the existing tests over the target (`poetry run pytest` at the relevant
   tier). Capture the real output. If they do not pass now, stop — you cannot
   distinguish "my refactor broke it" from "it was already broken."
2. Find what the existing tests actually pin — apply the `test-gap` skill's
   question: would they fail if the behavior changed? Untested behavior is
   behavior you can silently destroy, and it is exactly where refactors go
   wrong.
3. **Write characterization tests for the untested behavior in scope**, including
   the ugly parts. Assert what the code *currently does*, even where that looks
   wrong. Marking a quirk `# characterization: current behavior, see TODO #N` is
   how you preserve it deliberately instead of discovering later that something
   depended on it.
4. Note explicitly what remains uncovered — that is your risk surface, and it
   belongs in the final report.

## 3. Move in small, reversible steps

Sequence the work so the tests are green after **every** step. A refactor that
only compiles again at the end gives you no signal about which step broke it.

- Prefer mechanical, tool-assisted moves (rename, extract, inline) over
  hand-editing — they do not typo.
- One kind of change per step: move code, *or* rename, *or* change a signature.
  Combining them makes the diff unreadable.
- Run the tests after each step. When one goes red, the cause is the step you
  just took — that is the whole benefit of working small.
- Keep the formatter (`poetry run black`) separate. A formatting pass mixed into
  a structural change hides the structural change completely; if reformatting is
  needed, it is its own commit.

## 4. Verify behavior is genuinely unchanged

- The full relevant test tier passes, with output captured.
- **Read the final diff hunk by hunk and justify each one as behavior-preserving.**
  Any hunk you cannot justify that way is either a bug you introduced or a
  behavior change you did not intend to make. Both mean stop.
- Check specifically for the things that quietly change under a refactor:
  evaluation order, short-circuiting, exception type or message, mutation of a
  shared object, default values, iteration order, redaction/audit output, and
  any public REST/MCP surface shape.
- If a public surface, serialization, or persisted shape moved, that is not a
  pure refactor — it needs a compatibility path and a Decision Log entry.
- Confirm nothing was orphaned: no dead code left behind, no import left
  dangling, no doc still describing the old structure.

## 5. Finish it

- Run the `auditors` skill — `architecture-boundary-reviewer` especially, since
  the point of this work was structural. Triage every finding before the
  self-review.
- Docs: update `docs/PRODUCT_GUIDE.md` if a boundary or pattern changed (the
  `product-guide-sync` skill runs that check). Update comments and docstrings
  that describe the old structure — they are part of the refactor, not a
  follow-up.
- Commit the refactor **alone**. Any bug you found goes in a separate commit,
  with its own failing-first test. Branch first if on `main`.

## 6. Report

```
Motivation:   <the concrete problem this solved>
Scope:        <what moved> / <what you deliberately left alone>
Safety net:   <existing tests + characterization tests added>
Uncovered:    <behavior in scope that no test pins — the real risk>
Steps:        <the sequence, and that tests were green after each>
Behavior:     <confirmation the diff is behavior-preserving, and how you checked>
Found, not fixed: <bugs discovered and recorded separately — never fixed here>
Verified:     <commands run and their real results>
```

Then the CLAUDE.md report format with the honest 1–10. If you smuggled any
behavior change into this diff, say so plainly — that is the finding that matters
most here, and it is far cheaper to disclose than to have someone find it later
while bisecting.
