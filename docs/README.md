# The `docs/` map

Everything in this directory, what it is for, and who should read it. Start
with [the README](../README.md) if you have not already — this page assumes
you know what QueryGate is.

If you have ninety minutes and you are evaluating QueryGate, read in this
order: the README → [`LIMITATIONS.md`](LIMITATIONS.md) →
[`THREAT_MODEL.md`](THREAT_MODEL.md) → [`SECURITY_POSTURE.md`](SECURITY_POSTURE.md)
→ [`INFERENCE_RISKS.md`](INFERENCE_RISKS.md). Those three security documents are
the ones a reviewer is actually looking for, and they are written to be checked
rather than believed.

## Start here

| Document | What it is |
|---|---|
| [`FEATURE_REFERENCE.md`](FEATURE_REFERENCE.md) | The full capability tour with request/response examples — audit events, policy, masking, cost estimation, approvals, governed writes, quotas, the catalog, the config-governance API, the admin UI, templates, async execution, deployment. This used to be the bulk of the README. |
| [`LIMITATIONS.md`](LIMITATIONS.md) | The unabridged list of what is missing, partial, or opt-in. Read it before you form an opinion. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The package-by-package module map behind the README's pipeline diagram. |
| [`SECURITY_MODEL.md`](SECURITY_MODEL.md) | Every structural guarantee and the enforcement point for each. |
| [`PRODUCT_GUIDE.md`](PRODUCT_GUIDE.md) | The long-form explainer: architecture, terminology, every deliberate tradeoff, and the **Decision Log**. Large (~9,000 lines) — use its table of contents. [`product-guide.html`](product-guide.html) is a generated browsable twin (`make product-guide-html`). |

## For a security reviewer

| Document | What it is |
|---|---|
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | First-party threat model: threat-by-threat, with the control and the test that enforces it. |
| [`SECURITY_POSTURE.md`](SECURITY_POSTURE.md) | Every CI security gate, and the command to reproduce each one yourself. |
| [`INFERENCE_RISKS.md`](INFERENCE_RISKS.md) | What a *well-behaved* caller can still infer about denied data — including the risks accepted as residual rather than closed. |
| [`COMPLIANCE_MAPPING.md`](COMPLIANCE_MAPPING.md) | Product controls mapped to SOC 2 TSC and ISO/IEC 27001:2022 Annex A, with the evidencing artifact for each. Readiness mapping, **not** a certification. |
| [`TRUST_EVIDENCE.md`](TRUST_EVIDENCE.md) | Generated procurement packet composing the above plus current dependency-audit status. Regenerate with `make trust-page`. |
| [`SCOPE_CATALOG.md`](SCOPE_CATALOG.md) | Generated catalog of every authorization scope. Source of truth is `src/querygate/core/scopes.py`; regenerate with `make scope-catalog`. |
| [`../SECURITY.md`](../SECURITY.md) | How to report a vulnerability, what is in scope, and what happens next. |

## Operating and releasing it

| Document | What it is |
|---|---|
| [`RELEASING.md`](RELEASING.md) | The reproducible release process: gates, SBOM, dependency audit, container signing and provenance. |
| [`LOAD_TESTING.md`](LOAD_TESTING.md) | The real-Postgres load and soak harness for the concurrency and timeout guardrails. |
| [`THIRD_PARTY_LICENCES.md`](THIRD_PARTY_LICENSES.md) | Generated inventory of every locked Python package's licence. `make license-check` gates it deny-by-default. |
| [`CONTAINER_IMAGE_LICENCES.md`](CONTAINER_IMAGE_LICENCES.md) | The non-Python half of the shipped image (OS packages, the ODBC driver) that the Python inventory does not cover. |
| [`../deploy/README.md`](../deploy/README.md) · [`../deploy/runbook.md`](../deploy/runbook.md) | The two verified reference deployments (Docker Compose, Helm) and the operational runbook. |

## Licensing

**None of this is legal advice.** QueryGate is licensed under
**[Apache-2.0](../LICENSE)** — a real grant, not a notice. Everything in this
repository is free to use, modify and redistribute, including commercially and
inside closed-source products, under those terms.

| Document | What it is |
|---|---|
| [`../LICENSE`](../LICENSE) | The Apache-2.0 text. The grant lives here. |
| [`../COMMERCIAL.md`](../COMMERCIAL.md) | What is free forever (all of this repository, including the policy engine and the audit ledger — neither will ever be tier-gated), what is planned as a paid service, and the permanent non-goals. |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | Generated inventory of every locked Python package's licence, gated deny-by-default by `make license-check`. |
| [`CONTAINER_IMAGE_LICENCES.md`](CONTAINER_IMAGE_LICENCES.md) | The non-Python half of the shipped image, including Microsoft's ODBC driver, which ships under its own proprietary terms. |

## Design and planning documents (internal working notes)

These are working artifacts, not product documentation. They are kept in the
open because the reasoning is more useful than a clean surface, but do not read
them as statements of what ships today.

| Document | What it is |
|---|---|
| [`ENGINE_EXPRESSIVENESS_PLAN.md`](ENGINE_EXPRESSIVENESS_PLAN.md) | The flagship-pillar spec for the read query engine's expressiveness (TODO.md items 99–106). Authoritative for those items only. |
| [`STORED_PROCEDURE_CATALOG_PLAN.md`](STORED_PROCEDURE_CATALOG_PLAN.md) | A scoping proposal for TODO.md item 18. **Proposal only — not started, not approved.** |
| [`TODO_ARCHIVE.md`](TODO_ARCHIVE.md) | Full write-ups for every fully-shipped worklist item, keyed by its original number. Reference only; the live worklist is [`../TODO.md`](../TODO.md), sequenced by [`../ROADMAP.md`](../ROADMAP.md). Large (~14,000 lines). |
| [`mcp_extensions/`](mcp_extensions/) | The published `io.github.agitmit/structured-query-ast` MCP extension schema and its documentation. Regenerate with `make mcp-extension-schema`. |
| [`business/`](business/) | Mixed: reproducible benchmark methodology and results (`SECURITY_BENCHMARK.md`, `PERFORMANCE_BENCHMARK*.md`, `LOAD_BENCHMARK*.md`), which shipped CLIs and tests read; alongside commercial strategy and competitor material that is internal working reasoning, not product documentation. |

## Generated files — do not hand-edit

Four documents in this directory are generated from a source of truth and are
drift-tested; edit the source and regenerate instead.

| File | Regenerate with | Source of truth |
|---|---|---|
| `SCOPE_CATALOG.md` | `make scope-catalog` | `src/querygate/core/scopes.py` |
| `THIRD_PARTY_LICENSES.md` | `make license-report` | `poetry.lock` |
| `TRUST_EVIDENCE.md` | `make trust-page` | `SECURITY_POSTURE.md`, `COMPLIANCE_MAPPING.md`, `business/SECURITY_BENCHMARK.md`, `../SECURITY.md`, the dependency allowlist |
| `product-guide.html` | `make product-guide-html` | `PRODUCT_GUIDE.md` |
