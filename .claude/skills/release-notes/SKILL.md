---
name: release-notes
description: >-
  Turn shipped work into customer-facing release notes and update CHANGELOG.md.
  Use when cutting a release, when asked to "write release notes", "update the
  changelog", or after a batch of items ship. Translates internal item-N shorthand
  into end-user language.
---

# release-notes — changelog + customer-facing notes

`CHANGELOG.md` follows Keep-a-Changelog style with an `[Unreleased]` section and
`Added` / `Changed` / `Fixed` / `Security` groupings. Keep that structure.

## Source of truth

- Items marked ✅ DONE since the last release (`TODO.md` stubs +
  `docs/TODO_ARCHIVE.md` full write-ups).
- The git log since the last release tag.
- Cross-check against actually-shipped behavior — don't announce something that
  didn't fully land (watch for `✅ DONE (phase 1)` items: describe only the
  shipped phase).

## Procedure

1. Diff since the last released version (git tags; the current released tag is
   `v0.1.0` — **never** move or recreate it).
2. For each shipped change, write a **user-facing** line: what the customer can
   now do or trust, not the internal mechanism or item number. Keep a parenthetical
   `(item N)` reference only where the existing CHANGELOG already does so, for
   traceability — but lead with the user benefit.
3. Group under the right heading. **Security** is its own group and matters most
   for this product — surface hardening and invariant work there.
4. Add lines under `[Unreleased]`. On an actual version cut, move `[Unreleased]`
   contents under a new `## [X.Y.Z] - YYYY-MM-DD` heading and leave a fresh empty
   `[Unreleased]`.

## Style

- Plain language a customer or evaluator understands; no internal jargon.
- Accurate over impressive — a security product's changelog is read by security
  reviewers. Don't overstate.
- One line per change; link to docs where a change needs explanation.

## Guardrails

- Do not push, tag, or publish. Draft the notes; the human cuts the release.
- Don't invent version numbers or dates — ask if the target version isn't given.

## Report

The drafted CHANGELOG additions, the version/date assumption made (if any), and
any shipped item you deliberately omitted or hedged (e.g. partial phases).
