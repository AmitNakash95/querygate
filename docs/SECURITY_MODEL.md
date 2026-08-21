# Security model

The full list of structural guarantees and how each is enforced. The README
carries a seven-line summary of this page; this is the unabridged version.

Related: [`THREAT_MODEL.md`](THREAT_MODEL.md) (threat-by-threat control and
test mapping), [`SECURITY_POSTURE.md`](SECURITY_POSTURE.md) (every CI gate and
the command to run it yourself), [`INFERENCE_RISKS.md`](INFERENCE_RISKS.md)
(what a well-behaved caller can still infer), and
[`../SECURITY.md`](../SECURITY.md) (how to report a vulnerability).

## Guarantees

## Security model

- **No raw SQL, anywhere.** `StructuredQuery` has no `sql`/`query`-string
  field and rejects unknown fields (`extra="forbid"`) — there's no field to
  smuggle SQL into, and no endpoint that would accept it if there were.
- **Credentials never leave `connections/`.** `ConnectionProfile.connection_string`
  is never returned by any API/MCP response — `list_connections` and
  `GET /api/v1/connections` return `PublicConnectionInfo`, a separate model
  with no such field. This is asserted directly by
  `tests/unit/test_credential_redaction.py` against the live OpenAPI schema
  and MCP tool schemas, not just by convention.
- **Policy is enforced before compilation**, not as a post-hoc filter — a
  denied table/column, an over-cap query, or a disabled connection is
  rejected before a single line of SQL is built.
- **Every identifier is schema-checked**, not agent-asserted — a
  `Table.Column` reference that doesn't exist in the live reflected schema
  is rejected, regardless of what the AST claims.
- **Curated catalog metadata is policy-filtered too** — an optional schema
  catalog (business descriptions, relationship hints, sensitivity labels)
  never affects query enforcement, and never discloses a table/column a
  denied caller couldn't already see through ordinary schema discovery.
  Semantic search filters candidates—including both relationship columns—
  before ranking or counting and returns provenance/status/freshness on every
  hit.
- **Bounded execution** — every query runs under a per-connection
  concurrency semaphore and a policy-configured timeout; row counts are
  clamped server-side (tiered: lower for row selects, higher for
  aggregates), not left to the caller's `limit`.
- **Proactive cost estimation, not just reactive caps (Postgres)** — an
  optional `max_estimated_rows`/`max_estimated_cost` policy gate plans the
  compiled query with Postgres's own `EXPLAIN` before running it, and
  rejects likely full scans or join explosions before they ever touch real
  data instead of only bounding them once already running.
- **Auth is pluggable.** `core/auth.py` defines an `Authenticator` interface;
  static API keys and JWKS-verified OAuth/JWT bearer tokens are shared by REST
  and MCP without transport-specific authorization logic.
- **Secret resolution is pluggable and fails loud, not quiet.** `secrets/`
  defines a `SecretResolver` interface; a Vault error surfaces as a clear
  config-load failure that never echoes the configured Vault token or
  Vault's own response text — only what was being looked up and why it
  failed.
- **Audit trail** — every query attempt, successful or rejected, is logged
  to stdout and can be persisted as a narrow JSONL event. The persisted event
  contains identity, surface, normalized query shape, policy decision, timing,
  row/byte counts, and error category—never row payloads or query literals.
- **Config changes are versioned, attributed, and never silently applied.**
  Every `/admin/config/*` validate/preview/stage/apply/rollback is attributed to the
  calling principal, gated behind `admin:config:read`/`admin:config:write`,
  recorded as its own audit event (never the YAML content), and re-validated
  immediately before it takes effect — a version that fails validation is
  never activated, even if it validated when it was first staged.
- **Adversarially tested boundary** — denied identifiers cannot be smuggled
  through filters, joins, grouping, ordering, or ranking; undeclared tables
  cannot enter an implicit `FROM`; unexpected backend errors are masked; and
  MCP rejects unapproved Host headers. See [the threat model](THREAT_MODEL.md)
  and run `make test-security`.
- **Supply-chain transparency for the exact dependency set shipped** — every
  `make release-check` generates a CycloneDX SBOM and a `pip-audit`
  vulnerability report scoped to `poetry.lock`'s locked `main` group (not an
  unpinned resolve), plus SHA-256 checksums for the built artifacts. Any known
  vulnerability without a reviewed, justified entry in
  `security/dependency-audit-allowlist.json` fails the release
  (deny-by-default). Every `make release-check` also gates the **third-party
  licence inventory** of every locked Python package
  ([`docs/THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)): strong
  copyleft is blocking in either group and unwaivable, weak copyleft needs an
  individually recorded exception, and an unrecognised licence string fails
  rather than being guessed. See
  [`docs/RELEASING.md`](RELEASING.md#software-bill-of-materials-and-dependency-audit).
- **Signed, provenance-attested releases.** On a maintainer-pushed version
  tag, `.github/workflows/release.yml` builds and pushes the container image to
  GHCR behind a pre-publish Trivy gate, then **signs it with cosign keyless
  (Sigstore)** and attaches a **SLSA build-provenance attestation**
  (`actions/attest-build-provenance`) — both bound to the image digest and
  consumer-verifiable (`cosign verify` / `gh attestation verify`). Offline
  artifact integrity is checkable with `make verify-release`
  (`scripts/verify_release.py`) against `dist/SHA256SUMS`. Publishing is never
  automatic — it happens only when a maintainer deliberately pushes the tag.
- **Continuously scanned, and provable.** Every change runs static analysis
  (Bandit + Semgrep OSS), full-history secret scanning (gitleaks), container
  image scanning of the shipped image (Trivy), and OpenAPI fuzzing (Schemathesis)
  — each deny-by-default. For the full, reproducible security-and-reliability
  posture (every gate, the command to run it yourself, and the threat-model
  mapping) see **[docs/SECURITY_POSTURE.md](SECURITY_POSTURE.md)**. To report
  a vulnerability, see [`SECURITY.md`](../SECURITY.md).
