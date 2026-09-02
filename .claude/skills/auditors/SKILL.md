---
name: auditors
description: >-
  Run the applicable QueryGate reviewers in parallel over a working tree,
  commit, or commit range, then verify and triage every finding before
  committing or declaring a task complete. This is QueryGate's mandatory final
  completion gate for every top-level user task, including trivial, docs-only,
  and read-only tasks; when no reviewer applies, record that explicitly. Also
  use when asked to audit/review a change. Reviewers inspect security
  invariants, architecture boundaries, test contracts, UI accessibility, and
  public claims without editing the tree.
---

# auditors — parallel change review and triage

## Mandatory completion gate

Invoke this skill after implementation, documentation, and relevant tests are
finished, and before committing (when requested) or declaring any **top-level
user task** complete. Do this even for a trivial, docs-only, test-only, or
read-only task. A task with no applicable reviewer passes through the explicit
N/A path in step 2; skipping the skill is not the N/A path.

Reviewer subagents launched by this skill are internal stages of the parent
audit, not top-level user tasks. They must return their report directly to the
parent and must not invoke `auditors` recursively. The parent audit owns their
completion, triage, and final gate.

If any file changes after the audit—whether from an accepted fix, another tool,
or the user—repeat the affected review on the final tree before declaring
completion. Do not claim completion while a reviewer is still running, a
finding lacks a verdict, an accepted fix is unverified, or an owner decision is
still required.

Use `$ARGUMENTS` as an optional commit or range. With no arguments, audit the
uncommitted working tree; if it is clean, audit `HEAD`. For the mandatory
completion gate, use the task-start status baseline to distinguish this task's
delta from unrelated pre-existing work. List any excluded pre-existing paths.
If no reliable baseline exists, audit the whole working tree and state that the
scope could not be narrowed safely.

The reviewers look for defects compatible with a green suite: a new path around
the shared service, an AST shape enforced in one layer but not another, a
dialect branch outside its adapter, an authorization model that can contradict
itself, or a claim whose test proves something narrower than the prose.

## 1. Resolve and freeze the target

1. Read `CLAUDE.md`, especially the non-negotiables, architecture, composable
   interfaces, testing gotchas, and working agreement.
2. Resolve the target:
   - No args + dirty tree: include this task's staged, unstaged, and untracked
     delta. Exclude unrelated pre-existing work only when the task-start
     baseline makes that distinction reliable.
   - No args + clean tree: audit `HEAD`.
   - Args: validate the named commit or range before continuing.
3. Capture the target description, base/head commits when applicable,
   `git status --short`, changed-file name/status, and diff stat.
4. Read the diff to build an inventory. For every changed file, write one line
   stating what changed and what the file is responsible for. A generated or
   vendored file may be grouped with its source.
5. Identify the governing documents before launching reviewers:
   - Always: `CLAUDE.md` and the relevant `TODO.md` item, if any.
   - Security boundary/control: `docs/THREAT_MODEL.md`,
     `docs/SECURITY_POSTURE.md`, and the relevant section of
     `docs/PRODUCT_GUIDE.md`.
   - Product identity or a non-goal: `CLAUDE.md`’s "North Star" section.
   - AST/compiler work in items 99–106:
     `docs/ENGINE_EXPRESSIVENESS_PLAN.md`.
   - Release, packaging, deployment, or CI: `docs/RELEASING.md`.

Do not edit while reviewers run. The captured status is also the baseline for
detecting a moving working tree.

## 2. Select every applicable reviewer

Two reviewers is common; run all that match rather than forcing a fixed count.

| Reviewer | Run when the change… |
| --- | --- |
| `security-invariant-reviewer` | Touches an AST, policy/schema validation, compiler, execution, connections, auth/scopes, secrets, audit, catalog governance, config, admin access, a REST route, an MCP tool, a background task, import/export, or any public model. |
| `architecture-boundary-reviewer` | Adds/moves a module, changes layer ownership, introduces a model/schema/serialization shape, adds a backend/dialect/strategy, changes a Protocol/registry, or adds a representation of an identifier, principal, scope, limit, time, version, generation, or approval state. |
| `test-contract-reviewer` | Changes production behavior, tests, fixtures, CI gates, error handling, or dialect-specific behavior. |
| `ui-a11y-reviewer` | Touches `src/querygate/admin_ui/`, `src/querygate/access_ui/`, `landing/`, `sales/index.html`, or Python routes/templates that render a user-facing page. |
| `claim-reviewer` | Touches README/help/product/security/release docs, landing or sales copy, release notes, or behavior described by those surfaces. |

If none applies, name every reviewer as not applicable and stop. Do not invent a
review to make the audit look busy. This explicit result satisfies the mandatory
completion gate; silently omitting the audit does not.

Each reviewer's full standards, checklist, and report format live in its own
`.claude/agents/<reviewer-name>.md` — read the relevant one before briefing
that reviewer rather than restating its criteria here, so there is exactly one
place each reviewer's standard can drift out of date.

Require each selected reviewer to cover and explicitly mark these categories
clean or not clean:

- **Security:** raw-SQL/service bypass; cross-principal, cross-connection, and
  catalog isolation; auth/scope enforcement; credential/data/error/audit
  redaction; resource limits; single catalog mutation and draft quarantine.
- **Architecture:** pipeline/layer ownership; model consistency across parsing,
  policy, schema, compilation, audit, and public schemas; Protocol/registry
  dispatch; dialect semantics; async call sites and state lifecycle.
- **Tests:** rejection and error contracts; negative-path regression coverage;
  dialect parity; real-engine-only behavior; fixtures/singletons; whether the
  test genuinely fails when the behavior breaks.
- **UI:** semantics and accessible names; keyboard/focus; status/error feedback;
  contrast/motion/responsiveness; safe text rendering and bidirectional text.
- **Claims:** exact code and test backing; consistency across docs/help/marketing;
  shipped versus planned wording; reproducibility of security/release evidence.

## 3. Launch report-only reviewers in parallel

Launch each selected reviewer as its dedicated `subagent_type` — literally
`security-invariant-reviewer`, `architecture-boundary-reviewer`,
`test-contract-reviewer`, `ui-a11y-reviewer`, or `claim-reviewer`, matching the
agent definitions under `.claude/agents/`. Each of those definitions drops
`Write`/`Edit` from its tool list, closing the specific failure mode of a
generic subagent given only a report-only *instruction* while still holding
full tool access — don't fall back to a generic subagent for these roles.
This narrows the surface but does not make "report only" airtight: `Bash`
remains available and can itself write, move, or stage files (`>`, `sed -i`,
`git add`/`commit`, …), so the restriction reduces the blast radius of a
misbehaving reviewer rather than eliminating it. Treat it as raising the bar,
not as a guarantee that lets you skip briefing each reviewer not to touch the
tree.

Launch one subagent per selected reviewer in a single parallel batch, using
background execution when the runtime supports it. Do not serialize reviewers.
These reviewer subtasks are internal stages of this invocation: tell them not to
run `auditors` or any completion hook recursively.

Give each reviewer a tailored brief containing:

1. The exact target (working tree, commit, or range), including base/head.
2. The file-by-file inventory and each file's responsibility.
3. The governing documents to read **before** judging the design (its own
   `.claude/agents/<name>.md` already directs it to its skill-level standards
   — add anything target-specific here).
4. Numbered questions derived from the riskiest or least-certain choices in the
   diff. Ask about clever shortcuts and duplicated enforcement explicitly.
5. The categories the reviewer must mark clean when no issue is found.
6. This instruction:

   > Read the complete changed files and the adjacent call sites, models, and
   > tests; use the diff only as a locator. Do not run the test suite — your
   > tool access is already restricted to read-only tools. Separate
   > target-caused findings from pre-existing observations.

Require this response shape:

- `Finding ID — severity — title`
- Evidence with `file:line`
- The violated invariant/contract and concrete failure mode
- Why the current tests can remain green
- Smallest safe recommendation
- Explicit clean categories

Ask for defects, not style preferences. A grep hit is a lead, not a finding.

## 4. Hold edits, then reconcile

Wait for every reviewer. While waiting, write down open design questions, but do
not modify code, tests, or docs.

Before triage, compare the current status and target against the captured
baseline. If the tree moved, re-read every affected full file and rerun any
reviewer whose evidence may be stale. Never triage against code that no longer
exists.

Merge duplicate findings, then independently open and verify every cited site.
Reviewer severity is input, not the verdict.

## 5. Give every finding a verdict

Record one verdict and a reason for every finding:

- **Accept and fix now** — Confirmed defect, small and safe. Add the narrow
  regression test first and observe it fail for the reported reason, then fix
  the defect and run the test green. Preserve unrelated working-tree changes.
- **Accept, needs a decision** — Confirmed, but the remedy changes design,
  policy, a non-negotiable, persistence, migration, compatibility, or scope.
  Present a concrete proposal and stop on that finding for the owner. Do not
  implement it by judgment alone.
- **Disagree** — State the specific code, test, or governing decision that makes
  the finding incorrect. If the underlying concern is reasonable but behavior
  is deliberate, pin it with a focused test and a clear nearby comment/docstring
  when that clarification is small and safe.
- **Out of scope** — Pre-existing or unrelated. Link an existing `TODO.md` item
  or record a durable follow-up there using the repo's permanent-number and
  worklist rules. Put security residual-risk detail in
  `docs/THREAT_MODEL.md` when appropriate. Do not expose exploit details in a
  public tracking note and do not silently drop the finding.

Do not start fixes until every selected reviewer has reported and every finding
has an initial verdict.

## 6. Re-verify the final tree

After any accepted fix:

1. Run the new narrow regression test and the closest existing tests.
2. Run `make lint`.
3. Run the applicable tier:
   - `make test-unit` at minimum for production-code changes.
   - `make test-security` for pipeline, validation, auth, audit, catalog,
     credential, REST, or MCP security surfaces.
   - `make test-integration` for cross-module behavior.
   - The relevant Postgres/MSSQL/load target when correctness depends on a real
     engine; if unavailable, say so explicitly.
4. For a final pre-commit audit, run `release-gate` on the resulting tree.
   Fixes invalidate earlier green output for the old tree.
5. Re-read the final diff and confirm each accepted fix has a regression test
   that would fail without it.
6. Reconcile durable documentation:
   - Use `product-guide-sync` for architecture, tradeoff, terminology, or
     customer-visible behavior.
   - Update `docs/THREAT_MODEL.md` / `docs/SECURITY_POSTURE.md` if a security
     control or its evidence changed.
   - Update `SECURITY.md` only if vulnerability-reporting scope/process or a
     guarantee stated there changed.
   - Run `make worklist-sync` and `make worklist-check` after changing worklist
     status or adding a follow-up.

## 7. Report

Fold the audit into the standard self-review:

- Target and honest 1–10 rating.
- Reviewers run and why; reviewers not run and why.
- Each finding, its verdict, and reason.
- What was fixed immediately and its regression test.
- What needs the owner's decision, with a concrete proposal.
- Out-of-scope follow-ups and where they were recorded.
- Commands actually run and their result.
- Required or relevant checks not run, with the reason.
- Explicit confirmation that the mandatory completion gate is satisfied on the
  final tree.

Name clean reviewer categories explicitly. Silence is not evidence.
