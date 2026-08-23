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
from querygate.core.auth_policy import validate_methods


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

    def is_s3_worm_archived(self) -> bool:
        """Whether this backend archives events to the S3 WORM copy
        (TODO.md item 134 phase 2, `audit/worm_search.py`) — the capability
        gate the managed-search endpoint uses to decide whether there is an
        archive to search at all, instead of comparing against
        `JSONL_CHAINED_S3_WORM` inline at the route. Only one backend has
        this today, but the named method keeps the call site future-proof
        the same way `is_locally_readable()`/`wraps_events_in_a_hash_chain_
        envelope()` already do for the other two capabilities this enum
        publishes."""
        return self == AuditSinkBackend.JSONL_CHAINED_S3_WORM


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

    # TODO.md item 195: record the redaction-safe *shape* of each allowed
    # query, so an operator can see which shapes a principal actually uses
    # before narrowing that principal to curated templates
    # (`Policy.templates_only`). Opt-in and off by default, the same posture
    # as the usage signals above and the `min_group_size`/disclosure-budget
    # guardrails — a deployment that never intends to narrow a connection
    # should not pay for the recording. The store is always bounded; its SCOPE
    # depends on `concurrency_backend`: with `redis` it is shared across every
    # replica and durable across restarts (`admin/redis_observed_shapes.py`),
    # otherwise per serving process and volatile
    # (`admin/observed_shapes.py`). The report states which.
    observed_shapes_enabled: bool = pyd.Field(default=False)
    observed_shapes_max_entries: int = pyd.Field(default=500, ge=1, le=100_000)
    # TTL on the Redis-backed store's keys, refreshed on every write (30 days).
    # It bounds how long an *idle* window survives, so a deployment that stops
    # recording releases the memory. It does NOT expire an actively-written
    # window, however long — the refresh is unconditional on every record — so
    # this is not a cap on total discovery duration. Ignored by the in-process
    # store, which is volatile by nature. Upper bound is a year: a discovery
    # window is a cutover activity, and a longer retention is a job for an
    # audit sink, not this store.
    observed_shapes_ttl_seconds: int = pyd.Field(default=2_592_000, ge=60, le=31_536_000)

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
    # Which identity.yaml provider's claim→scope rules apply to bearer JWTs, so
    # one human gets the same authority whether they sign in through the browser
    # or their agent presents an IdP token. Empty (the default) leaves JWT scope
    # resolution exactly as it was: the `scope` claim and nothing else.
    jwt_mapping_provider_id: str = pyd.Field(default="")

    # --- Human SSO (TODO.md item 199) ---------------------------------------
    # The `jwt_*` block above authenticates a bearer token an agent or service
    # already holds. This block is about a *person* signing in: the OIDC
    # authorization-code flow behind the admin/access UIs, QueryGate's built-in
    # local identity provider, and the device grant that hands an SSO identity
    # to a CLI. All of it is off by default; enabling it changes no existing
    # credential path, and `identity/mapping.py` is deny-by-default, so turning
    # SSO on grants nobody anything until an operator writes a mapping rule.
    sso_enabled: bool = pyd.Field(default=False)
    # Providers, SSO settings, and the claim→scope mapping (identity/config_store.py).
    identity_file: str = pyd.Field(default="identity.yaml")
    # QueryGate's own username/password (+ optional TOTP) identity provider, for
    # deployments with no external IdP and for break-glass access when the IdP
    # is unreachable. Requires SSO_ENABLED — it is served through the same
    # session surface, not as a second auth path.
    local_idp_enabled: bool = pyd.Field(default=False)
    local_users_file: str = pyd.Field(default="users.yaml")
    # Consecutive failures before an account stops answering, and for how long.
    # This — not the KDF — is what bounds online password guessing.
    local_login_lockout_threshold: int = pyd.Field(default=5, ge=0, le=1000)
    local_login_lockout_seconds: float = pyd.Field(default=900, ge=0, le=86400)
    # Refuse a local sign-in from an account that has not enrolled a TOTP factor.
    local_require_mfa: bool = pyd.Field(default=False)
    sso_session_cookie_name: str = pyd.Field(default="qg_session")
    # Secure-only cookie. Refused in production if turned off (see the validator):
    # a session cookie sent over cleartext is a session handed to the network.
    # Secure-only by default, but see `_relax_local_cookie` below: a local
    # deployment served over plain http gets `false` unless it says otherwise,
    # because a Secure cookie is silently DROPPED by the browser there — the
    # login redirects successfully and the person is anonymous on the next
    # request, which looks like a QueryGate bug rather than a cookie policy.
    sso_session_cookie_secure: bool = pyd.Field(default=True)
    sso_session_cookie_samesite: str = pyd.Field(default="lax")
    # Header the UI echoes the session's CSRF token in. Required on EVERY
    # cookie-authenticated request (identity/authenticators.py), not just
    # unsafe methods.
    sso_csrf_header: str = pyd.Field(default="X-QueryGate-CSRF")
    # RFC 8628 device grant: let a CLI obtain a short-lived token carrying the
    # approving human's identity. Off by default; requires SSO_ENABLED.
    sso_device_grant_enabled: bool = pyd.Field(default=False)
    sso_device_code_ttl_seconds: float = pyd.Field(default=600, ge=60, le=1800)
    sso_device_token_ttl_seconds: float = pyd.Field(default=3600, ge=60, le=86400)
    # Serve QueryGate's OWN OpenID Connect provider at <base_url>/dev-idp, so the
    # real sign-in flow runs with no external IdP to register (identity/dev_idp.py).
    # This is an authentication bypass by design: it vouches for anyone who clicks.
    # It is refused outside a local environment, both here and in the router
    # builder, so it cannot be shipped by accident.
    dev_idp_enabled: bool = pyd.Field(default=False)

    # --- Which credential types each surface accepts (item 200) -------------
    # Authentication says who a caller is; this says whether that *kind* of
    # proof is acceptable here. Valid entries: api_key, jwt, sso_session,
    # sso_device_token, anonymous. An unknown name is refused at load.
    #
    # Empty means "use the default for that surface" — and the console's
    # default is not permissive: with SSO_ENABLED=true it drops `api_key` and
    # `anonymous`, so integrating an identity provider actually closes the
    # shared-secret door to the control plane instead of adding a second one
    # beside it. Set this explicitly to keep a static key working there.
    console_auth_methods: list[str] = pyd.Field(default_factory=list)
    rest_auth_methods: list[str] = pyd.Field(default_factory=list)
    mcp_auth_methods: list[str] = pyd.Field(default_factory=list)

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
    # Optional S3-API endpoint override (TODO.md item 201). Empty (the
    # default) means AWS S3 proper, resolved by region as before. Set it to a
    # MinIO / Ceph RGW endpoint to keep the WORM tier — and therefore the
    # "even we can't delete it" claim — available on-prem and in air-gapped
    # deployments, which is the Reach pillar, not cloud breadth. Both the
    # flush monitor and the managed search read this same value, so an
    # archive is never written to one store and searched in another.
    #
    # The store must implement S3 Object Lock: this is an endpoint override,
    # NOT an "any object store" adapter. No CI job or live test in this repo
    # runs against a non-AWS store, so any specific store's support is a
    # vendor claim rather than something QueryGate has verified. Retention is still sent as
    # `ObjectLockMode`/`ObjectLockRetainUntilDate`, so pointing this at a
    # store without Object Lock would archive objects that are ordinary,
    # deletable blobs while the deployment still believed they were WORM —
    # `app.py` warns at startup for exactly that reason. Google Cloud Storage
    # and Azure Blob are NOT reachable this way: their immutability models
    # are their own APIs, not S3 Object Lock (see PRODUCT_GUIDE).
    audit_worm_s3_endpoint_url: str = pyd.Field(default="")
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

    # Managed search over the WORM archive (TODO.md item 134 phase 2,
    # audit/worm_search.py). Reads the same bucket/prefix/region the flush
    # monitor writes to, above — a search request never touches a second
    # store. All bounds below exist so a caller cannot make one request scan
    # an unbounded slice of a potentially years-long compliance archive; a
    # request outside them is rejected (4xx), never silently served slow.
    #
    # Max width of one requested [start, end) window. Default (~2 years)
    # deliberately covers the "18 months back" compliance-review scenario
    # this feature exists for, while still being a real, enforced ceiling.
    audit_worm_search_max_window_days: int = pyd.Field(default=730, ge=1)
    # Hard per-request cap on S3 objects (segments) fetched — the real cost
    # driver of a scan, since each is a network round trip. Hit mid-scan, the
    # response is truncated (not an error) with a cursor to resume, the same
    # "stop and disclose honestly" posture admin/anomaly.py's max_lines_read
    # uses for its own bounded reader.
    audit_worm_search_max_objects_scanned: int = pyd.Field(default=2000, ge=1)
    # Page size bounds. `limit` on a request is clamped to this ceiling by
    # the route/tool layer, never silently raised.
    audit_worm_search_default_limit: int = pyd.Field(default=50, ge=1)
    audit_worm_search_max_limit: int = pyd.Field(default=500, ge=1)
    # Wall-clock budget for one request's S3 work. Checked between object
    # fetches (never mid-fetch), so a request degrades to a truncated,
    # resumable page rather than hanging past this bound.
    audit_worm_search_request_timeout_seconds: float = pyd.Field(default=20.0, gt=0)

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
    # TODO.md item 141: see `admin.anomaly.AnomalyThresholds.
    # max_consecutive_out_of_window`'s own docstring for the full rationale
    # and residual risk (docs/THREAT_MODEL.md QG-43). Exposed as its own
    # config field (rather than only the model default) so an operator who
    # knows their deployment is merged/multi-writer can raise it or set it to
    # a very large value to effectively disable the early exit.
    anomaly_max_consecutive_out_of_window: int = pyd.Field(default=5_000, ge=1)
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
    # TODO.md item 141: see `anomaly_max_consecutive_out_of_window` above for
    # the full rationale — same knob, independently tunable for this reader.
    change_trend_max_consecutive_out_of_window: int = pyd.Field(default=5_000, ge=1)

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
    # TODO.md item 141: same rationale as `anomaly_max_consecutive_out_of_window`
    # — kept independently tunable for the same reason `max_lines_read` above
    # is (this surface is reachable with authentication only, no admin scope).
    personal_denials_max_consecutive_out_of_window: int = pyd.Field(default=5_000, ge=1)
    personal_denials_limit: int = pyd.Field(default=20, ge=1)
    # TODO.md item 126: this is the only self-service (no-admin-scope) REST
    # surface whose handler does an O(file-size) scan of the persisted audit
    # JSONL per call — bound repeated calls per-caller the same way item 43's
    # admin_connection_test_cooldown_seconds bounds "test now" probes. 0
    # disables the cooldown (every other self-service endpoint's posture).
    # Disclosed limits (2026-08-10 security-invariant-reviewer, item 126's own
    # completion gate): the cooldown is per-WORKER-PROCESS (num_of_workers > 1
    # or multiple replicas multiply the effective ceiling — same limitation
    # execution/quota.py's in-process limiter has), and its bucket is
    # `principal.subject`, which every statically configured API key shares
    # one of (see docs/THREAT_MODEL.md QG-33) — real per-caller granularity
    # needs JWT auth.
    personal_denials_cooldown_seconds: float = pyd.Field(default=5.0, ge=0)

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

    # TODO item 135: proactive, TTL/lease-driven credential re-resolution.
    # Item 13 already made a rotated `${vault:...}` value take effect on the
    # next reload without a restart; this closes the remaining gap that the
    # refresh was operator-pull only. Off by default and additive — it only
    # ever *triggers* the existing reload_config()/dispose_engine() path
    # earlier, proactively, for a reference whose resolver reports a lease
    # (secrets/resolvers.py's LeasedSecretResolver); a deployment with no
    # leased resolver registered behaves identically to today.
    credential_lease_refresh_enabled: bool = pyd.Field(default=False)
    credential_lease_check_interval_seconds: float = pyd.Field(default=60, gt=0)
    credential_lease_refresh_margin_seconds: float = pyd.Field(default=300, gt=0)

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
        "console_auth_methods",
        "rest_auth_methods",
        "mcp_auth_methods",
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
        if (
            self.is_local
            and self.sso_enabled
            and "sso_session_cookie_secure" not in self.model_fields_set
        ):
            # Only when the operator did not state a preference: an explicit
            # `SSO_SESSION_COOKIE_SECURE=true` on localhost is honoured (someone
            # testing behind a TLS proxy), and production still refuses `false`
            # outright in the check further down. This default flips for
            # localhost/development only.
            object.__setattr__(self, "sso_session_cookie_secure", False)
        for field_name in ("console_auth_methods", "rest_auth_methods", "mcp_auth_methods"):
            # Validates the names only. A resolved allowlist can never be empty
            # — an empty configured list falls back to that surface's default,
            # and a non-empty one is non-empty by construction — so there is no
            # lock-everyone-out case to guard here, and inventing one would be
            # a security check that never fires.
            validate_methods(getattr(self, field_name), field=field_name.upper())
        if self.local_idp_enabled and not self.sso_enabled:
            raise ValueError(
                "SSO_ENABLED must be true when LOCAL_IDP_ENABLED=true — the local identity "
                "provider is served through the SSO session surface, not as a separate path"
            )
        if self.sso_device_grant_enabled and not self.sso_enabled:
            raise ValueError("SSO_ENABLED must be true when SSO_DEVICE_GRANT_ENABLED=true")
        if self.dev_idp_enabled:
            if not self.sso_enabled:
                raise ValueError("SSO_ENABLED must be true when DEV_IDP_ENABLED=true")
            if not self.is_local:
                # The whole point of this provider is that it authenticates
                # anybody. Refusing to start is the only safe response to finding
                # it enabled anywhere a real person could reach it.
                raise ValueError(
                    "DEV_IDP_ENABLED=true is only permitted when ENVIRONMENT is local — "
                    "it serves an identity provider that vouches for any caller"
                )
        if self.sso_enabled and self.sso_session_cookie_samesite.lower() not in (
            "lax",
            "strict",
        ):
            # `none` would make the session cookie usable cross-site, which is
            # precisely what SameSite is here to prevent; it is not offered.
            raise ValueError("SSO_SESSION_COOKIE_SAMESITE must be 'lax' or 'strict'")
        if (
            self.environment == "production"
            and self.sso_enabled
            and not self.sso_session_cookie_secure
        ):
            raise ValueError(
                "SSO_SESSION_COOKIE_SECURE must not be disabled when ENVIRONMENT=production"
            )
        if self.concurrency_backend == ConcurrencyBackend.REDIS and not self.concurrency_redis_url:
            raise ValueError("CONCURRENCY_REDIS_URL must be set when CONCURRENCY_BACKEND=redis")
        if self.vault_enabled and not self.vault_addr:
            raise ValueError("VAULT_ADDR must be set when VAULT_ENABLED=true")
        if self.vault_enabled and not self.vault_token:
            raise ValueError("VAULT_TOKEN must be set when VAULT_ENABLED=true")
        if self.credential_lease_refresh_enabled and not self.vault_enabled:
            # `VaultSecretResolver` is the only registered LeasedSecretResolver
            # implementation today — enabling this without Vault would start
            # a background loop with zero registered resolvers capable of
            # reporting a lease, a misconfiguration worth failing loudly on
            # rather than silently doing nothing. This does not guarantee a
            # *nonzero* lease will ever be reported (the shipped Vault
            # integration reads KV v2 static secrets, whose lease_duration is
            # genuinely 0) — only that at least one resolver capable of
            # reporting one in principle is registered.
            raise ValueError(
                "VAULT_ENABLED must be true when CREDENTIAL_LEASE_REFRESH_ENABLED=true "
                "(no other registered secret resolver currently reports a lease)"
            )
        if (
            self.credential_lease_refresh_enabled
            and self.credential_lease_check_interval_seconds
            >= self.credential_lease_refresh_margin_seconds
        ):
            raise ValueError(
                "CREDENTIAL_LEASE_CHECK_INTERVAL_SECONDS must be smaller than "
                "CREDENTIAL_LEASE_REFRESH_MARGIN_SECONDS, or a lease could come due and expire "
                "between polls without the monitor ever seeing it inside the margin window"
            )
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
