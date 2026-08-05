# Compliance control mapping (SOC 2 / ISO 27001 readiness)

This document maps QueryGate's **product controls** to the SOC 2 Trust Service
Criteria (2017 TSC, Common Criteria) and the corresponding ISO/IEC 27001:2022
Annex A controls, with a pointer to the concrete artifact — code, test, CI gate,
or doc — that evidences each one. It exists so a regulated-industry buyer's
security team can answer "does this satisfy control X?" against real evidence
instead of a marketing claim (TODO.md item 54).

**Read the scope boundary first — it is the honest part.** QueryGate is a
**self-hosted product component**, not a SaaS. A SOC 2 report or ISO 27001
certificate is issued to an *organization* for a *system operated over a period*,
not to a piece of software. So this mapping is deliberately split:

- **Product-provided** — a control QueryGate implements and evidences in this
  repository. These are the rows a customer can inherit or point to.
- **Shared responsibility** — QueryGate provides the mechanism; the deploying
  organization must configure and operate it (e.g. point audit at durable
  storage, set the least-privileged DB account).
- **Customer/organization responsibility** — a control that is entirely the
  deploying org's to run (HR screening, physical security, an incident-response
  *process*, the audit engagement itself). QueryGate can't and shouldn't claim
  these; they are listed so nothing is silently missing.

Nothing here asserts QueryGate "is SOC 2 certified" or "is ISO 27001 certified."
It asserts which controls the product *supports with evidence* so the
certification effort starts from a mapped baseline rather than a blank page.

---

## SOC 2 Common Criteria mapping

### CC1 — Control Environment (governance, roles)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC1.1–1.4 Integrity, board oversight, org structure, competence | Org-level — QueryGate is a component, not the operating entity. | — | Customer/org |
| CC1.3 Roles & responsibilities for the system | Least-privilege authorization scopes with recommended role bundles (Analyst/Operator/Config Governor/Catalog Author/Catalog Admin/Data Steward). | `core/scopes.py`, `docs/SCOPE_CATALOG.md` (item 95) | Product-provided |

### CC2 — Communication & Information

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC2.1 Quality information (audit trail) | Redaction-safe structured audit log + append-only JSONL sink; every request attempt is recorded. | `audit/logger.py`, `audit/sinks.py`; `tests/unit/test_credential_redaction.py` (item 23) | Product-provided |
| CC2.2/2.3 Internal & external communication of responsibilities | Threat model documents the deployment trust boundary and the operator's obligations. | `docs/THREAT_MODEL.md` §7, `deploy/README.md` | Product-provided |

### CC3 — Risk Assessment

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC3.1–3.4 Identify & assess risks, including fraud & change | First-party threat model with assets, attacker capabilities, threats→controls, residual risks, and review triggers. | `docs/THREAT_MODEL.md` | Product-provided |
| CC3.4 Assess changes | Governed config-change management: every change is staged, validated against the same runtime rules, versioned, attributed to the calling principal, and roll-back-able. | `admin/service.py`, `admin/store.py` (item 25) | Product-provided |

### CC4 — Monitoring Activities

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC4.1 Ongoing evaluation | Adversarial security regression suite + boundary fuzzing run in CI on every change; published safety benchmark. | `tests/security/`, `make test-security` (items 28/36/55); `querygate-security-benchmark` (item 58) | Product-provided |
| CC4.1 Operational monitoring | Prometheus metrics + aggregated observability endpoint (query volume, rejection categories, queue/concurrency pressure). | `metrics.py`, `GET /metrics`, `admin/observability.py` (items 12/44) | Shared responsibility |

### CC5 — Control Activities

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC5.1–5.3 Control selection, technology controls, policies | The entire request pipeline is a deny-by-default control: policy caps + allow/deny, schema validation, no raw-SQL path, per-dialect session guardrails. | `validation/policy_validation.py`, `validation/schema_validation.py`, `compiler/`, `connections/dialects.py` | Product-provided |

### CC6 — Logical & Physical Access Controls

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC6.1 Logical access — identity | Pluggable authN: static API keys + JWKS-verified JWT, composable; delegated (on-behalf-of) identity via RFC 8693. | `core/auth.py` (items 8/10/90) | Product-provided |
| CC6.1 Logical access — authorization | Per-principal, per-connection table/column policy keyed by `sub`/claim; scope-gated admin operations; mandatory row filters. | `policy/models.py`, `policy/loader.py`, `core/scopes.py` | Product-provided |
| CC6.1 Least privilege (data) | Column masking/tokenization; deny-by-default policy; least-privileged read-only DB account required by deployment. | item 49 masking; `docs/THREAT_MODEL.md` §7 | Product + Shared |
| CC6.2/6.3 Provisioning & de-provisioning | Identity lifecycle is the IdP's; QueryGate consumes it. Scope→role catalog makes provisioning a role assignment. | `docs/SCOPE_CATALOG.md` (item 95) | Shared responsibility |
| CC6.6 Boundary protection | No raw-SQL/DML input path anywhere; TLS terminated at the operator's proxy; admin/metrics endpoints restricted at the network layer. | `README.md`, `docs/THREAT_MODEL.md` §7; `tests/unit/test_credential_redaction.py` | Product + Shared |
| CC6.7 Restrict data transmission | Credentials never returned on any public model; audit events never carry SQL/values/rows/credentials. | `connections/models.py` (`PublicConnectionInfo`), `audit/sinks.py`; `test_credential_redaction.py` | Product-provided |
| CC6.8 Malicious software / integrity | Signed, provenance-attested release artifacts (cosign keyless + SLSA build provenance, digest-bound); SBOM; dependency & image scanning. | `.github/workflows/release.yml`, `make verify-release` (items 30/89); `docs/SECURITY_POSTURE.md` | Product-provided |
| CC6.4/6.5 Physical access & disposal | Deploying org / cloud provider. | — | Customer/org |

### CC7 — System Operations

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC7.1 Vulnerability detection | SAST (Bandit + Semgrep OSS), dependency audit (deny-by-default allowlist), container image scan, secret scanning, DAST — all CI gates. | `docs/SECURITY_POSTURE.md`, `make sast` + `make semgrep`/dep-audit (item 30) | Product-provided |
| CC7.2 Monitoring for anomalies | Read-only behavioral anomaly surfacing on the audit stream. | `admin/anomaly.py` (item 59) | Product-provided |
| CC7.3/7.4 Incident response | Tamper-evident, hash-chained audit ledger + per-query receipts give the forensic record; the IR *process* is the org's. | `audit/ledger.py`, `querygate-audit verify` (item 91) | Product + Org |
| CC7.5 Recovery | HA/DR reference deployment + backup/restore runbook with RTO/RPO. | `deploy/HA_DR.md` (item 56) | Product + Shared |

### CC8 — Change Management

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC8.1 Authorized, tested, approved changes | Release gates (`make release-check`/`release-smoke`) for product changes; governed config changes with validation, versioning, attribution, and rollback for runtime changes. (Four-eyes approval + policy simulation/diff/blast-radius are roadmap items 42/39/40/41, not yet shipped — see gaps.) | `docs/RELEASING.md` (item 24); `admin/service.py` (item 25) | Product-provided |

### CC9 — Risk Mitigation

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC9.1 Risk mitigation | Concurrency limits, timeouts, result-size caps, per-principal quotas, query-cost gating protect the operational DB. | `execution/concurrency.py`, `execution/quota.py`, `execution/cost_estimation.py` | Product-provided |
| CC9.2 Vendor & third-party management | Dependency allowlist + SBOM is the software-supply-chain half; vendor-management *process* is the org's. | `docs/SECURITY_POSTURE.md`; dep-audit | Product + Org |

## Confidentiality (C-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| C1.1 Identify & protect confidential data | Column masking/tokenization, deny-by-default column policy, redaction-safe audit. | item 49; `audit/sinks.py` | Product-provided |
| C1.2 Disposal / retention | Catalog retention/deletion + export (least-privilege scopes); audit retention has an opt-in native WORM path (S3 Object Lock COMPLIANCE mode) or remains operator-configured storage otherwise. | items 32B-2 (`catalog:delete`/`catalog:export`); item 134 (`AUDIT_SINK_BACKEND=jsonl_chained_s3_worm`); `deploy/HA_DR.md` (audit storage) | Product + Shared |

## Availability (A-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| A1.1/1.2 Capacity & recovery | Multi-replica HA deployment, autoscaling, PDB, zero-downtime rolling updates, DR runbook. | `deploy/helm/querygate/`, `deploy/HA_DR.md` (item 56) | Product + Shared |
| A1.3 Recovery testing | Failover-drill checklist provided; the drill is operator-run against their cluster. | `deploy/HA_DR.md` §5 | Shared responsibility |

## Processing Integrity (PI-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| PI1.1–1.3 Inputs/processing are complete, valid, accurate | Every input is a validated `StructuredQuery` AST against policy + live schema before any DB touch; no raw-SQL path; property-based compiler fuzzing. | request pipeline; `tests/unit/test_compiler_properties.py` (item 36) | Product-provided |

---

## ISO/IEC 27001:2022 Annex A cross-reference (key domains)

| Annex A control | QueryGate evidence |
|---|---|
| A.5.15 Access control | `policy/`, `core/scopes.py`, `docs/SCOPE_CATALOG.md` |
| A.5.16 Identity management | `core/auth.py` (JWT/JWKS, delegated identity) |
| A.5.17 Authentication information | Secrets via env/Vault resolver, never on public models (`secrets/`, `connections/models.py`) |
| A.8.2/8.3 Privileged access & information access restriction | Scope-gated admin ops; per-principal column/row policy |
| A.8.8 Management of technical vulnerabilities | SAST, dep-audit, image scan, secret scan, DAST (`docs/SECURITY_POSTURE.md`) |
| A.8.15 Logging | Redaction-safe structured audit + hash-chained ledger (`audit/`) |
| A.8.16 Monitoring activities | Metrics, observability endpoint, anomaly surfacing |
| A.8.24 Use of cryptography | HMAC-SHA256 audit chaining; cosign-signed releases; TLS at the operator's edge |
| A.8.28 Secure coding | Deny-by-default pipeline, no raw SQL, adversarial regression suite |
| A.8.31 Separation of environments | Config governance staging vs. live; four-eyes approval |
| A.5.7 Threat intelligence / A.5.24-5.28 Incident management | Anomaly surfacing + tamper-evident ledger feed the org's IR process |

---

## Genuine gaps (real, not process theater)

Per this item's rule — close only real gaps, don't manufacture process around
controls that already exist — the following are the honest remainder. They are
**organizational**, not product code, so QueryGate cannot "implement" them; they
are what a deploying org completes to reach an actual report/certificate:

1. **The audit engagement itself.** A SOC 2 Type II report or ISO 27001
   certificate requires an independent auditor and a control-operating period.
   This is coordination-gated (see also item 53, third-party security audit). No
   code closes it.
2. **Organizational controls (CC1, CC6.4/6.5, parts of CC7.3/CC9.2):** HR
   screening/onboarding, physical/environmental security, the incident-response
   *process and on-call*, vendor-management *process*, and a formal access-review
   *cadence*. QueryGate provides the technical evidence these processes act on
   (audit trail, scope catalog, anomaly stream) but the recurring human process
   is the deploying org's.
3. **Access-review evidence formalization.** QueryGate exposes the inputs
   (scope catalog, per-principal policy, audit of admin actions); turning those
   into a scheduled, signed-off access review is an org process to stand up.
4. **Config-change separation-of-duties (product roadmap, not yet shipped).**
   Four-eyes config approval (item 42), draft-aware policy simulation (item 39),
   semantic access diff (item 40), and blast-radius analysis (item 41) will
   further strengthen CC8.1 change management once shipped. Today, change
   integrity rests on release gates + the governed config plane (item 25); these
   four are enhancements, not a claimed-but-missing control.

The product-side of item 54 — the mapping and the honest gap analysis above — is
complete. Items 53 (auditor) and any org-process standup are the human/vendor
remainder.
