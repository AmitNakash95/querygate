"""Application-level settings.

No database connection strings live here — those belong to individual
connection profiles (see querygate/connections/), loaded from a separate
file so secrets never mix with app config or get serialized into a schema.
"""

from __future__ import annotations

import json
from enum import Enum
from importlib import resources
from typing import Any, Literal, Optional

import pydantic as pyd
from pydantic_settings import BaseSettings

from querygate import __version__
from querygate.catalog.providers import SemanticMemoryProviderMode


class ConcurrencyBackend(str, Enum):
    """Concurrency guardrail backend (execution/concurrency.py).

    IN_PROCESS (default) is an asyncio.Semaphore per connection id — correct
    for one instance only. REDIS enforces Policy.max_concurrency across every
    instance sharing one Redis (see execution/redis_concurrency.py).
    """

    IN_PROCESS = "in_process"
    REDIS = "redis"


class AuditSinkBackend(str, Enum):
    """Persisted audit destination. Stdout audit logging remains always on."""

    NONE = "none"
    JSONL = "jsonl"
    # Tamper-evident hash-chained JSONL ledger (TODO.md item 91). Same event
    # bodies as JSONL, each wrapped in a chain envelope so edits/deletions/
    # reordering are detectable via `querygate-audit verify`.
    JSONL_CHAINED = "jsonl_chained"
    # Composes the local hash-chained ledger above with an additional,
    # asynchronously-flushed WORM (S3 Object Lock) archival copy for
    # compliance-grade retention (TODO.md item 134). The local file is
    # unchanged by this — every existing local reader keeps working exactly
    # as it does for JSONL_CHAINED; WORM is a parallel durable copy, not a
    # replacement (audit/sinks.py's CompositeAuditSink).
    JSONL_CHAINED_S3_WORM = "jsonl_chained_s3_worm"

    def is_locally_readable(self) -> bool:
        """Whether QueryGate's own read surfaces (the personal-denials
        report, the anomaly report, the config/catalog change-trend report,
        and the admin UI audit browser — TODO.md item 136) can read this
        backend's persisted stream back off local disk. Both JSONL variants
        and the WORM-composed variant share one underlying local file format
        — `JSONL_CHAINED`/`JSONL_CHAINED_S3_WORM` wrap each event in a hash-
        chain envelope that readers transparently unwrap via
        `audit.ledger.unwrap_envelope` — so all three are readable; `NONE`
        has nothing persisted to read. The single capability lookup every
        such gate must use instead of an equality/inequality check against
        one member, so a future backend declares its readability once here
        rather than at every call site."""
        return self in (
            AuditSinkBackend.JSONL,
            AuditSinkBackend.JSONL_CHAINED,
            AuditSinkBackend.JSONL_CHAINED_S3_WORM,
        )

    def wraps_events_in_a_hash_chain_envelope(self) -> bool:
        """Whether this backend's persisted local file wraps each event in a
        `LedgerRecord` envelope (TODO.md item 91) that a reader must unwrap
        before reading the event body — as opposed to plain JSONL, one event
        object per line. The second single-capability lookup TODO.md item 136
        introduced `is_locally_readable()` for: every `require_envelope=...`
        call site must use this instead of comparing against
        `AuditSinkBackend.JSONL_CHAINED` alone, so a future envelope-wrapping
        backend (this item added `JSONL_CHAINED_S3_WORM`) doesn't silently
        read as `require_envelope=False` and misreport every event's shape."""
        return self in (
            AuditSinkBackend.JSONL_CHAINED,
            AuditSinkBackend.JSONL_CHAINED_S3_WORM,
        )


class MetricsHistoryBackend(str, Enum):
    """Time-windowed metrics history source for the observability dashboard
    (TODO.md item 44, phase 2). NONE (default) means QueryGate's own
    in-process snapshot (admin/observability.py) is the only trend surface;
    PROMETHEUS queries an operator-configured Prometheus-compatible HTTP API
    for real history and cross-replica aggregation, since QueryGate owns no
    time-series store of its own."""

    NONE = "none"
    PROMETHEUS = "prometheus"


def _parse_str_list(value: Any) -> Any:
    """Accept a JSON array, a comma-separated string, or a list as-is."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            return json.loads(value)
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _example_file(name: str) -> str:
    """Resolve bundled example config in both a checkout and an installed wheel."""
    return str(resources.files("examples").joinpath(name))


class AppConfig(BaseSettings):
    environment: Literal["production", "staging", "development", "localhost"] = pyd.Field(
        default="localhost"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = pyd.Field(default="INFO")
    # nosec B104: binds all interfaces *inside* the container by design;
    # network exposure is controlled at the deployment boundary.
    host_address: str = pyd.Field(default="0.0.0.0")  # nosec B104
    port: int = pyd.Field(default=8000)
    app_version: str = pyd.Field(default=__version__)
    num_of_workers: int = pyd.Field(default=1)
    api_v1_prefix: str = pyd.Field(default="/api/v1")

    # Connection profiles and policy are file-configured, not code-configured —
    # see examples/connections.example.yaml and examples/policy.example.yaml.
    # Connection strings are resolved via ${ENV_VAR} interpolation inside the
    # connections file, so secrets live only in the environment.
    connections_file: str = pyd.Field(
        default_factory=lambda: _example_file("connections.example.yaml")
    )
    policy_file: str = pyd.Field(default_factory=lambda: _example_file("policy.example.yaml"))

    # Optional curated schema-catalog overlay (querygate/catalog/) — business
    # descriptions, aliases, relationship hints, and sensitivity metadata fed
    # into describe_table. Unset by default: a deployment with no curated
    # catalog behaves identically, just without the extra `catalog` field.
    catalog_file: Optional[str] = pyd.Field(default=None)

    # Optional admin-defined query templates (querygate/templates/, TODO.md
    # item 48) — named, parameterized `StructuredQuery` skeletons agents invoke
    # by name. Unset by default: a deployment with no templates behaves
    # identically, just without the template list/run surface.
    # The documented env var is the plural TEMPLATES_FILE (see .env.example and
    # examples/templates.example.yaml); without this alias the field name would
    # only bind the singular TEMPLATE_FILE, so the documented var was silently
    # ignored — same AliasChoices pattern as api_keys/mcp_api_keys.
    template_file: Optional[str] = pyd.Field(
        default=None, validation_alias=pyd.AliasChoices("TEMPLATES_FILE", "template_file")
    )

    # Governed semantic-memory enrichment (catalog/). Provider execution is
    # deliberately limited to disabled/manual-only in 32A-2; no networked
    # provider implementation is shipped. Row-free schema refresh is a
    # separate opt-in background task and never runs on the query path.
    semantic_memory_provider: SemanticMemoryProviderMode = pyd.Field(
        default=SemanticMemoryProviderMode.DISABLED
    )
    semantic_memory_refresh_enabled: bool = pyd.Field(default=False)
    semantic_memory_refresh_interval_seconds: float = pyd.Field(default=300, gt=0)
    semantic_memory_refresh_max_tables: int = pyd.Field(default=500, ge=1, le=5000)

    # TODO item 32C: redaction-safe usage signals + background usage
    # learning. Both are opt-in and off by default, mirroring refresh above.
    # Signal *recording* into the in-process buffer is best-effort and never
    # touches the catalog file lock on the query path; only the background
    # monitor below batches buffered signals into the catalog file.
    semantic_memory_usage_signals_enabled: bool = pyd.Field(default=False)
    semantic_memory_usage_signal_buffer_size: int = pyd.Field(default=5000, ge=1, le=100_000)
    semantic_memory_learning_enabled: bool = pyd.Field(default=False)
    semantic_memory_learning_interval_seconds: float = pyd.Field(default=3600, gt=0)

    # REST and MCP share one API-key authenticator (see core/auth.py). Both
    # allow an anonymous dev-bypass outside production when no keys are set.
    api_keys: list[str] = pyd.Field(
        default_factory=list, validation_alias=pyd.AliasChoices("API_KEYS", "api_keys")
    )
    api_key_subject: str = pyd.Field(default="api-key-client")
    # Scopes granted to every caller authenticating via `api_keys` — all keys
    # in the list currently share one subject, so scopes are uniform too.
    # Per-key/per-caller differentiation needs a richer Authenticator (JWT).
    api_key_scopes: list[str] = pyd.Field(default_factory=list)

    mcp_enabled: bool = pyd.Field(default=False)
    mcp_mount_path: str = pyd.Field(default="/mcp")
    mcp_api_keys: list[str] = pyd.Field(
        default_factory=list, validation_alias=pyd.AliasChoices("MCP_API_KEYS", "mcp_api_keys")
    )
    mcp_api_key_subject: str = pyd.Field(default="mcp-service-account")
    mcp_api_key_scopes: list[str] = pyd.Field(default_factory=list)
    # Protect locally reachable MCP servers from browser-driven DNS rebinding.
    # Add the deployment's exact proxy/public Host header in production.
    mcp_dns_rebinding_protection: bool = pyd.Field(default=True)
    mcp_allowed_hosts: list[str] = pyd.Field(
        default_factory=lambda: [
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
        ]
    )
    mcp_allowed_origins: list[str] = pyd.Field(default_factory=list)
    # TODO.md item 86: transport-level request-body guards for the mounted MCP
    # Streamable-HTTP surface, so an oversized or absurdly deep body is a clean
    # client error (413/400) *before* the transport's own json.loads — matching
    # REST's "malformed input is never a 5xx" posture. Both defaults are far
    # above any legitimate batch (a policy-bounded query nests well under 10;
    # max_where_depth defaults to 5), so normal traffic is unaffected.
    mcp_max_request_bytes: int = pyd.Field(default=4 * 1024 * 1024, ge=1)
    mcp_max_request_depth: int = pyd.Field(default=100, ge=1)

    # MCP OAuth 2.0 resource-server conformance (TODO.md item 90 phase 2), per the
    # MCP 2026-07-28 authorization spec. Opt-in and off by default: when disabled,
    # the MCP surface behaves exactly as before. When enabled the mounted MCP
    # surface becomes a proper OAuth resource server —
    #   * publishes RFC 9728 protected-resource metadata at
    #     `/.well-known/oauth-protected-resource<MCP_MOUNT_PATH>`,
    #   * enforces RFC 8707 audience binding (a verified token must be issued for
    #     `mcp_resource_identifier`, blocking confused-deputy token reuse), and
    #   * answers a missing/invalid credential or an insufficient scope with an
    #     RFC 6750 `WWW-Authenticate` challenge pointing back at that metadata,
    #     so a client can perform the RFC 8693 token exchange / step-up.
    # Requires jwt_enabled (the delegated-identity resolver from phase 1).
    mcp_oauth_resource_server_enabled: bool = pyd.Field(default=False)
    # The canonical resource URI clients name as the token audience (RFC 8707).
    # Published as `resource` in the metadata and required in each token's `aud`.
    mcp_resource_identifier: str = pyd.Field(default="")
    # Authorization-server issuer identifiers advertised in the metadata so a
    # client knows where to obtain a correctly-audienced token.
    mcp_authorization_servers: list[str] = pyd.Field(default_factory=list)
    # Scopes a caller must hold to use the MCP surface. Empty = no scope gate
    # (audience binding still applies). A caller missing any of these gets a
    # 403 `insufficient_scope` challenge naming the required scopes.
    mcp_required_scopes: list[str] = pyd.Field(default_factory=list)
    # Optional human-facing documentation URL advertised in the metadata.
    mcp_resource_documentation: str = pyd.Field(default="")
    # In-query approval gate (item 92): allow an MCP client's human to approve a
    # sensitive/expensive read *in the querying session* via MCP elicitation,
    # instead of the out-of-band REST `query:approve` token flow. Off by default
    # (deny-by-default): an elicitation response carries no authenticated
    # approver identity, so enabling this is an explicit decision that the
    # client's human is a trusted approver — bypassing the scope separation REST
    # enforces. Leave it off to force REST-token-only approval. Only takes effect
    # when a policy's approval gate is enabled *and* approval_token_hmac_key is set.
    mcp_elicitation_approval_enabled: bool = pyd.Field(default=False)

    # JWT bearer-token auth (core/jwt_auth.JwtAuthenticator) — a second,
    # optional Authenticator alongside the static api_keys above. Shared by
    # both REST and MCP (one identity provider for both surfaces); when
    # enabled, a caller may authenticate with either a configured API key or
    # a valid JWT (see core/auth.CompositeAuthenticator).
    jwt_enabled: bool = pyd.Field(default=False)
    jwt_jwks_url: str = pyd.Field(default="")
    jwt_issuer: str = pyd.Field(default="")
    jwt_audience: str = pyd.Field(default="")
    jwt_algorithms: list[str] = pyd.Field(default_factory=lambda: ["RS256"])
    jwt_subject_claim: str = pyd.Field(default="sub")
    jwt_scopes_claim: str = pyd.Field(default="scope")
    # RFC 8693 delegation (TODO.md item 90): the claim carrying the actor (the
    # agent acting on behalf of `jwt_subject_claim`, the human). When present on
    # a verified token, the human's policy applies and both identities are
    # audited. Standard name is `act`; configurable for non-standard IdPs.
    jwt_act_claim: str = pyd.Field(default="act")
    jwt_leeway_seconds: float = pyd.Field(default=0)

    # How often each enabled connection is pinged in the background for
    # GET /health's readiness signal (see querygate/health.py).
    health_check_interval_seconds: float = pyd.Field(default=30)

    # GET /metrics (TODO.md item 144, docs/THREAT_MODEL.md QG-36) already
    # labels rejections "policy" vs "schema" for existing execute/explain
    # traffic — the same distinction the verdict endpoint's response body
    # deliberately collapses (QG-34). Secure by default: a caller needs
    # admin:metrics:read to scrape it, same Authenticator/scope machinery as
    # every other admin surface. Set false only when the endpoint's network
    # reachability is already restricted (e.g. a sidecar-only scrape path)
    # and an operator has made that tradeoff deliberately.
    metrics_require_auth: bool = pyd.Field(default=True)

    # Minimum interval between two manually triggered "test now" probes
    # (item 43 phase 2) against the same connection. A manual probe opens a
    # real connection to the target database on demand, so this bounds how
    # often an admin:connections:test caller can trigger one, independent of
    # the background interval above. Single-process visibility only, the
    # same caveat as execution/concurrency.py's in-process semaphore.
    admin_connection_test_cooldown_seconds: float = pyd.Field(default=10)

    # Persisted audit events are separate from the always-on structured
    # stdout audit log. JSONL is append-only and intended for a persistent
    # volume or collection by the customer's log/SIEM agent.
    audit_sink_backend: AuditSinkBackend = pyd.Field(default=AuditSinkBackend.NONE)
    audit_jsonl_path: str = pyd.Field(default="var/audit/querygate-audit.jsonl")
    audit_jsonl_fsync: bool = pyd.Field(default=False)
    # Optional secret keying the hash-chained ledger (audit_sink_backend=
    # jsonl_chained, TODO.md item 91). Set it to make the chain HMAC-SHA256 —
    # tamper-evident against anyone with file access but not the key. Empty
    # leaves the chain unkeyed (SHA-256): still detects corruption/reorder/
    # truncation, but tamper-evidence then relies on externally anchoring the
    # head hash. Never logged; verification (`querygate-audit verify --hmac-key`)
    # needs the same value.
    audit_ledger_hmac_key: str = pyd.Field(default="")
    # TODO.md item 138: hard bound on total *lines read from disk* per admin
    # UI audit-browser page request, independent of `limit`/`cursor` — bounds
    # worst-case parse/validate work on an oversized or adversarial file. The
    # reader (`api/admin_ui_routes.py`'s `_audit_page`) scans tail-first
    # (`audit.file_reader.iter_lines_reverse`), so this cap is hit only after
    # every genuinely recent line has already been seen.
    audit_page_max_lines_read: int = pyd.Field(default=200_000, ge=1)

    # Compliance-grade WORM audit retention (TODO.md item 134), active only
    # when audit_sink_backend=jsonl_chained_s3_worm. Composes with (never
    # replaces) the local hash-chained ledger above: the local file is
    # unchanged, and this configures the ADDITIONAL S3 Object Lock archival
    # copy. Buffered/batched and flushed off the request path — see
    # audit/worm_sink.py's module docstring for the fail-open rationale.
    audit_worm_s3_bucket: str = pyd.Field(default="")
    audit_worm_s3_prefix: str = pyd.Field(default="querygate-audit/")
    audit_worm_s3_region: str = pyd.Field(default="")
    # S3 Object Lock retention mode. COMPLIANCE cannot be shortened or
    # removed by anyone, including the AWS account root — the stronger
    # guarantee a regulated buyer's "prove nobody could have deleted this"
    # question needs. GOVERNANCE allows a specifically-permissioned principal
    # to override it, which weakens the "even we can't delete it" claim this
    # feature exists to make, so COMPLIANCE is the default; GOVERNANCE is
    # opt-in for an operator who has a documented, deliberate reason to want
    # an escape hatch.
    audit_worm_retention_mode: str = pyd.Field(default="COMPLIANCE")
    audit_worm_retention_days: int = pyd.Field(default=180, ge=1)
    # A batch (never a single event — see the Decision Log's object-
    # granularity rationale) is flushed when either bound is hit, whichever
    # comes first.
    audit_worm_flush_interval_seconds: float = pyd.Field(default=60, gt=0)
    audit_worm_max_buffered_events: int = pyd.Field(default=5000, ge=1)

    # HMAC key that signs in-query approval tokens (execution/approval.py,
    # TODO.md item 92). Empty (the default) means the approval gate cannot issue
    # or verify tokens — a deployment that sets Policy.approval_max_estimated_*
    # MUST set this, otherwise a triggered query can never be approved
    # (fail-closed by design). Never logged; the token binds a query fingerprint
    # + expiry, never a query value or secret.
    approval_token_hmac_key: str = pyd.Field(default="")

    # Read-only per-principal anomaly surfacing over the persisted audit stream
    # (TODO.md item 59). Purely a signal for a human admin — never wired into
    # enforcement. Requires a locally-readable audit_sink_backend (jsonl or
    # jsonl_chained — AuditSinkBackend.is_locally_readable()); with backend=none
    # the anomaly endpoint honestly reports source="disabled". A caller's recent
    # window is compared against its own preceding baseline window.
    anomaly_recent_window_seconds: float = pyd.Field(default=3600.0, gt=0)
    anomaly_baseline_window_seconds: float = pyd.Field(default=86400.0, gt=0)
    anomaly_min_baseline_events: int = pyd.Field(default=20, ge=1)
    anomaly_min_recent_events: int = pyd.Field(default=5, ge=1)
    anomaly_volume_spike_ratio: float = pyd.Field(default=3.0, gt=1)
    anomaly_rejection_rate_delta: float = pyd.Field(default=0.3, gt=0, le=1)
    anomaly_max_events_scanned: int = pyd.Field(default=200_000, ge=1)
    # TODO.md item 138: hard bound on total *lines read from disk*, independent
    # of how many are retained — see `admin.anomaly.AnomalyThresholds.max_lines_read`.
    anomaly_max_lines_read: int = pyd.Field(default=200_000, ge=1)
    anomaly_max_principals_reported: int = pyd.Field(default=100, ge=1)

    # Config/catalog-change trend surfacing over the persisted audit stream
    # (TODO.md item 44, phase 2 slice). Same recent-vs-baseline shape as the
    # anomaly windows above, applied to config.governance/catalog.governance
    # events fleet-wide rather than query.execution events per-principal.
    # Requires a locally-readable audit_sink_backend (jsonl or jsonl_chained);
    # with backend=none the endpoint honestly reports source="disabled".
    change_trend_recent_window_seconds: float = pyd.Field(default=3600.0, gt=0)
    change_trend_baseline_window_seconds: float = pyd.Field(default=86400.0, gt=0)
    change_trend_max_events_scanned: int = pyd.Field(default=200_000, ge=1)
    # TODO.md item 138: hard bound on total *lines read from disk*, independent
    # of how many are retained — see `admin.config_trends.ChangeTrendThresholds.max_lines_read`.
    change_trend_max_lines_read: int = pyd.Field(default=200_000, ge=1)

    # Time-windowed metrics history for the observability dashboard (TODO.md
    # item 44, phase 2 remainder) — queries an *operator-configured* external
    # metrics backend for real trend charts, since item 44 phase 1's
    # process-snapshot registry has no stored history to plot. Default backend
    # "none" keeps the endpoint honest (source="disabled"); set backend to
    # "prometheus" and prometheus_url to a reachable Prometheus HTTP API (one
    # already scraping this deployment's /metrics) to enable it. QueryGate only
    # ever reads from this backend — it never writes to it.
    metrics_history_backend: MetricsHistoryBackend = pyd.Field(default=MetricsHistoryBackend.NONE)
    metrics_history_prometheus_url: str = pyd.Field(default="")
    metrics_history_window_seconds: float = pyd.Field(default=6 * 3600.0, gt=0)
    metrics_history_step_seconds: float = pyd.Field(default=60.0, gt=0)
    # Upper bound on points returned per series — bounds the request even if
    # an operator configures a wide window with a tiny step, by widening the
    # effective step rather than ever returning an unbounded series.
    metrics_history_max_points_per_series: int = pyd.Field(default=500, ge=1)
    metrics_history_request_timeout_seconds: float = pyd.Field(default=5.0, gt=0)

    # Safe explanations of the caller's own recent denials (TODO.md item 45,
    # phase 2) — a principal-scoped, self-service read of the persisted audit
    # stream reached from GET /help/my-recent-denials. Requires a
    # locally-readable audit_sink_backend (jsonl or jsonl_chained); with
    # backend=none the endpoint honestly reports source="disabled".
    personal_denials_lookback_seconds: float = pyd.Field(default=86400.0, gt=0)
    personal_denials_max_events_scanned: int = pyd.Field(default=50_000, ge=1)
    # TODO.md item 138: hard bound on total *lines read from disk*, independent
    # of how many are retained. Kept independently tunable (rather than
    # inheriting anomaly_max_lines_read) and defaulted tighter than the
    # admin-scoped surfaces, since this is the one reachable with
    # authentication only, no admin scope, by design (item 45).
    personal_denials_max_lines_read: int = pyd.Field(default=50_000, ge=1)
    personal_denials_limit: int = pyd.Field(default=20, ge=1)

    # Config-governance version history (querygate/admin/) — staged/applied/
    # rolled-back snapshots of connections.yaml/policy.yaml/catalog.yaml,
    # separate from the files AppConfig itself points at (which the existing
    # POST /admin/reload-config keeps reloading unchanged, for infra-as-code
    # deployments that edit files directly rather than through this API).
    config_governance_dir: str = pyd.Field(default="var/config_versions")

    # Four-eyes config approval (TODO.md item 42). 0 (default) = single-
    # administrator mode: a staged version can be applied by any
    # `admin:config:write` holder, exactly as before. When >= 1, a staged
    # version cannot be applied until it has at least this many `approve`
    # decisions from DISTINCT reviewers holding `admin:config:approve`, none of
    # whom is the version's author (separation of duties enforced server-side,
    # never only in the UI).
    require_config_approvals: int = pyd.Field(default=0, ge=0)
    # Upper bound on an imported config change-set bundle (item 47). A bundle
    # carries only submitted document deltas plus a base fingerprint, so this
    # is far above any legitimate change set; it bounds the import endpoint so
    # a hostile/oversized upload is a clean client error, never an OOM.
    config_bundle_max_bytes: int = pyd.Field(default=1024 * 1024, ge=1)

    # Server-side encrypted-at-rest draft store (TODO.md item 47, phase 2) —
    # an optional alternative to the phase-1 download/upload bundle, for
    # full-config recovery (including a `connections` document, which may
    # carry a literal credential) that survives a lost download and works
    # across devices. Empty key (the default) means the subsystem is
    # disabled: the REST endpoints fail closed with a clean 503 rather than
    # ever writing plaintext. Never a second config-mutation path — a loaded
    # draft still flows through the unchanged validate/stage/apply plane.
    draft_store_dir: str = pyd.Field(default="var/drafts")
    draft_store_encryption_key: str = pyd.Field(default="")
    draft_store_retention_seconds: float = pyd.Field(default=7 * 86400.0, gt=0)
    draft_store_max_drafts_per_principal: int = pyd.Field(default=20, ge=1)

    concurrency_backend: ConcurrencyBackend = pyd.Field(default=ConcurrencyBackend.IN_PROCESS)
    concurrency_redis_url: str = pyd.Field(default="")
    # Should comfortably exceed the longest legitimate query (policy
    # timeout + scheduling/network slack) — a slot held past this is
    # assumed to belong to a crashed instance and is reclaimed.
    concurrency_redis_lease_seconds: float = pyd.Field(default=120)
    concurrency_redis_poll_interval_seconds: float = pyd.Field(default=0.05)
    # True (default) prioritizes availability: a Redis outage degrades to
    # unenforced concurrency rather than rejecting every query. False fails
    # closed instead — set it for deployments where an unenforced
    # concurrency cap on the underlying database is the worse outcome.
    concurrency_redis_fail_open: bool = pyd.Field(default=True)

    # HashiCorp Vault-backed secret resolution (querygate/secrets/) — an
    # alternative to ${ENV_VAR} for connection strings that shouldn't live in
    # the environment at all, selectable per-reference via a `${vault:...}`
    # scheme prefix in connections.yaml (env var interpolation keeps working
    # unchanged either way). Token auth only for this first backend.
    vault_enabled: bool = pyd.Field(default=False)
    vault_addr: str = pyd.Field(default="")
    vault_token: str = pyd.Field(default="")
    vault_kv_mount: str = pyd.Field(default="secret")
    vault_namespace: str = pyd.Field(default="")

    # Engine pool defaults, shared across connections (per-connection timeout /
    # concurrency guardrails live in policy, not here).
    pool_size: int = pyd.Field(default=20)
    conn_max_overflow: int = pyd.Field(default=10)
    pool_timeout: int = pyd.Field(default=30)
    pool_recycle: int = pyd.Field(default=3600)
    odbc_driver: str = pyd.Field(default="ODBC+Driver+17+for+SQL+Server")
    db_trust_server_certificate: bool = pyd.Field(default=False)

    @pyd.field_validator(
        "api_keys",
        "mcp_api_keys",
        "api_key_scopes",
        "mcp_api_key_scopes",
        "mcp_allowed_hosts",
        "mcp_allowed_origins",
        "mcp_authorization_servers",
        "mcp_required_scopes",
        "jwt_algorithms",
        mode="before",
    )
    @classmethod
    def _parse_key_lists(cls, value: Any) -> Any:
        return _parse_str_list(value)

    @pyd.model_validator(mode="after")
    def _validate_production_auth(self) -> "AppConfig":
        if self.environment == "production" and not self.api_keys and not self.jwt_enabled:
            raise ValueError("API_KEYS or JWT_ENABLED must be set when ENVIRONMENT=production")
        if (
            self.environment == "production"
            and self.mcp_enabled
            and not self.mcp_api_keys
            and not self.jwt_enabled
        ):
            raise ValueError(
                "MCP_API_KEYS or JWT_ENABLED must be set when ENVIRONMENT=production "
                "and MCP_ENABLED=true"
            )
        if self.jwt_enabled and not self.jwt_jwks_url:
            raise ValueError("JWT_JWKS_URL must be set when JWT_ENABLED=true")
        if self.mcp_oauth_resource_server_enabled:
            if not self.jwt_enabled:
                raise ValueError(
                    "JWT_ENABLED must be true when MCP_OAUTH_RESOURCE_SERVER_ENABLED=true "
                    "(audience binding is enforced on verified tokens)"
                )
            if not self.mcp_resource_identifier:
                raise ValueError(
                    "MCP_RESOURCE_IDENTIFIER must be set when "
                    "MCP_OAUTH_RESOURCE_SERVER_ENABLED=true"
                )
            if not self.mcp_authorization_servers:
                raise ValueError(
                    "MCP_AUTHORIZATION_SERVERS must list at least one issuer when "
                    "MCP_OAUTH_RESOURCE_SERVER_ENABLED=true"
                )
        if self.concurrency_backend == ConcurrencyBackend.REDIS and not self.concurrency_redis_url:
            raise ValueError("CONCURRENCY_REDIS_URL must be set when CONCURRENCY_BACKEND=redis")
        if self.vault_enabled and not self.vault_addr:
            raise ValueError("VAULT_ADDR must be set when VAULT_ENABLED=true")
        if self.vault_enabled and not self.vault_token:
            raise ValueError("VAULT_TOKEN must be set when VAULT_ENABLED=true")
        if self.semantic_memory_refresh_enabled and not self.catalog_file:
            raise ValueError("CATALOG_FILE must be set when SEMANTIC_MEMORY_REFRESH_ENABLED=true")
        if self.semantic_memory_usage_signals_enabled and not self.catalog_file:
            raise ValueError(
                "CATALOG_FILE must be set when SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED=true"
            )
        if self.semantic_memory_learning_enabled and not self.catalog_file:
            raise ValueError("CATALOG_FILE must be set when SEMANTIC_MEMORY_LEARNING_ENABLED=true")
        return self

    @property
    def is_local(self) -> bool:
        return self.environment in ("localhost", "development")

    class Config:
        case_sensitive = False
        env_file = ".env"
        extra = "allow"


config = AppConfig()
