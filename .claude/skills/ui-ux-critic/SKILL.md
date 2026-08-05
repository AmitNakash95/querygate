---
name: ui-ux-critic
description: >-
  Hands-on product-design critique of QueryGate's live admin_ui/access_ui —
  drives a real headless browser through the screens in scope and reports
  precise, fix-anchored UI/UX findings. Use when asked to review, critique, or
  improve the UI/UX, "how does this look", or before/after a UI-heavy change
  ships. Complements the static `ui-a11y-reviewer` used by `auditors`; never a
  replacement for it. Launches the `ui-ux-critic` subagent on a design-caliber
  model by default; a different or more expensive model only with the user's
  explicit confirmation this session.
---

# ui-ux-critic — a real reviewer's eyes on the running app

This is a **hands-on** review, not a code read — `architecture-boundary-reviewer`
and `ui-a11y-reviewer` already check architecture and accessibility statically
as part of `auditors`; this exists for what a diff can't show: whether the
thing actually looks and feels right when it's running. Use it standalone, or
alongside `auditors` on a UI-heavy change — never as a replacement for it.

The surfaces in scope are QueryGate's `admin_ui` and `access_ui` — plain
static HTML/CSS/JS mounted at `/admin` and `/access` by the FastAPI app. This
skill does not apply to `landing/` or `sales/index.html` (marketing pages,
covered by `claim-reviewer`/`pitch-sync` instead) unless the user specifically
asks for a design critique of those too.

## 1. Scope

`$ARGUMENTS` names the screen(s) or flow to review. Empty or "all" means every
screen currently reachable in `admin_ui` and `access_ui` — resolve the actual
list by reading `src/querygate/admin_ui/index.html`/`app.js` and
`src/querygate/access_ui/index.html`/`app.js` now; don't reuse a list from a
previous run, since the app changes between sessions.

State the resolved scope back to the user in one line before proceeding.

## 2. Model — a design-caliber model by default, anything else only with approval

Spawn the `ui-ux-critic` subagent with an explicit `model` on the `Agent`
call — never omit it and let it inherit the parent session's model, since the
point is a model capable of real design judgment doing the looking. Default to
`opus` (matches the agent's own frontmatter).

**Only consider a different or more expensive model if the user asks for it**
in `$ARGUMENTS` or the conversation. If so, confirm with `AskUserQuestion`
first — one question, the default model vs. the requested one — even if it
was approved in an earlier session; that approval doesn't carry forward.
Don't ask this question unprompted.

## 3. Bring up a real target

Start QueryGate the way CLAUDE.md documents:
`poetry run uvicorn querygate.api.app:app --reload` (or `make run-dev`),
default `http://localhost:8000`. State plainly that you're doing this — it's
routine and reversible, but still a visible act (a process, a port), not a
silent one.

If the scope includes authenticated screens (most of `admin_ui`/`access_ui`
require a bearer token entered through a login modal), ask the user for a way
in — an API key from their local connection/auth config — rather than
inventing or bypassing one. If the scope is only the unauthenticated login
screen itself, no key is needed.

## 4. Prepare a scratch directory

Create a fresh subdirectory under this session's scratchpad for screenshots
and any accessibility/console captures. Nothing from this review belongs in
the repo.

## 5. Brief the subagent thoroughly

Launch `Agent` with `subagent_type: "ui-ux-critic"` and the model resolved in
step 2. Give it, concretely:

- The base URL to use (`http://localhost:8000` unless told otherwise) and
  which routes need the authenticated session.
- The resolved route list from step 1.
- The viewport pairs to use — its own instructions default to a desktop
  viewport plus a real mobile-engine viewport, since QueryGate has no
  documented mobile-responsiveness commitment to follow instead.
- The scratch directory path from step 4.
- The API key to use for authenticated screens, if the user provided one.
- Anything specific the user flagged ("the connection list feels cramped",
  "does the approval flow make sense") — pass their exact words through;
  don't paraphrase away the signal.

Run it in the **foreground** (`run_in_background: false`) unless the user
wants to keep working on something else in the meantime — this skill's whole
output is the review, so there's usually nothing to overlap it with.

## 6. Present the findings

Relay the subagent's report to the user as-is: findings ranked by severity,
each with the screen, evidence (screenshot path + `file:line` where found),
concrete user impact, and a specific fix — plus what was reviewed and found
clean, and what couldn't be verified.

This skill **reports, it does not fix**. If the user wants a finding acted on,
that's a separate, explicit follow-up — implement it as its own task, sized
and audited per CLAUDE.md's working agreement (a single spacing/token fix is
trivial; a redesigned information architecture needs the owner's decision
before you start it, and likely a `docs/PRODUCT_GUIDE.md` Decision Log entry
if it changes an established pattern).

The review itself is read-only and ships nothing, but CLAUDE.md's completion
gate still applies — run `auditors` per its mandatory-even-for-read-only-work
rule; expect every reviewer to come back N/A since nothing changed in the
tree. No `TODO.md`/`ROADMAP.md` entry is needed for the review itself. Only
the follow-up work, if any is taken on, goes through the normal task
lifecycle.
