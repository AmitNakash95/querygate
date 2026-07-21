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
    host_address: str = pyd.Field(default="0.0.0.0")
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
    jwt_leeway_seconds: float = pyd.Field(default=0)

    # How often each enabled connection is pinged in the background for
    # GET /health's readiness signal (see querygate/health.py).
    health_check_interval_seconds: float = pyd.Field(default=30)

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

    # Config-governance version history (querygate/admin/) — staged/applied/
    # rolled-back snapshots of connections.yaml/policy.yaml/catalog.yaml,
    # separate from the files AppConfig itself points at (which the existing
    # POST /admin/reload-config keeps reloading unchanged, for infra-as-code
    # deployments that edit files directly rather than through this API).
    config_governance_dir: str = pyd.Field(default="var/config_versions")

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
