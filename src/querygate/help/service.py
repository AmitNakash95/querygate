"""Shared guide service used by REST and MCP adapters."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Literal, Optional, Type

import pydantic as pyd
import yaml
from pydantic_core import PydanticUndefined

from querygate import __version__
from querygate.admin import service as governance
from querygate.admin.models import EffectiveGuardrails, MandatoryFilterReadiness
from querygate.catalog.models import SchemaCatalog
from querygate.connections.models import ConnectionProfile
from querygate.connections.visibility import list_visible_connections, resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import AuthorizationError, NotFoundError, PolicyViolationError
from querygate.core.scopes import (
    ADMIN_CONFIG_READ_SCOPE,
    ADMIN_CONFIG_WRITE_SCOPE,
    ADMIN_RELOAD_CONFIG_SCOPE,
)
from querygate.help.corpus import GuideCorpus, get_guide_corpus
from querygate.help.models import (
    AccessSummary,
    CallerCapabilities,
    ConfigFieldExplanation,
    ConfigVersionSummary,
    ConnectionAccessDetail,
    ErrorExplanation,
    GuideCitation,
    GuideSearchHit,
    GuideSearchResponse,
    GuideTopic,
    GuideTopicResponse,
    PolicyRuleCounts,
    RedactedCatalogSummary,
    RedactedConfiguration,
    RedactedConnection,
    RedactedPolicyConfiguration,
    RedactedPolicySummary,
    SetupChecklistResponse,
    SetupStep,
)
from querygate.policy.models import Policy

_REFERENCE_RE = re.compile(r"^\$\{([^}]+)\}$")

_MODEL_ALIASES: dict[str, Type[pyd.BaseModel]] = {
    "app": AppConfig,
    "connection": ConnectionProfile,
    "policy": Policy,
    "catalog": SchemaCatalog,
}

_MODEL_TOPICS = {
    "app": "setup.first-run",
    "connection": "configuration.connections",
    "policy": "configuration.policy",
    "catalog": "configuration.catalog",
}

_FIELD_DESCRIPTIONS = {
    ("connection", "connection_string"): (
        "SQLAlchemy connection URL. Store it behind an environment or external-secret "
        "reference; QueryGate never returns its configured or resolved value."
    ),
    ("connection", "known_tables"): (
        "Optional seed names used for schema discovery before live reflection has populated "
        "the cache."
    ),
    ("policy", "enabled"): (
        "Controls whether this policy exposes the connection. Disabled and unknown "
        "connections are intentionally indistinguishable to callers."
    ),
    ("policy", "mandatory_row_filters"): (
        "Row predicates QueryGate inserts into every matching query, optionally resolving "
        "their value from an authenticated principal claim."
    ),
    ("app", "api_key_scopes"): "Scopes granted to callers authenticated by a REST API key.",
    ("app", "mcp_api_key_scopes"): "Scopes granted to callers authenticated by an MCP API key.",
    ("app", "config_governance_dir"): (
        "Directory containing immutable staged configuration versions and the active pointer."
    ),
    ("app", "semantic_memory_provider"): (
        "Semantic draft provider mode. Defaults to disabled; 32A also supports only strict "
        "offline manual imports and has no networked model adapter."
    ),
    ("app", "semantic_memory_refresh_enabled"): (
        "Starts opt-in background row-free schema refresh. Requires a writable persistent "
        "catalog file and never runs on the query request path."
    ),
    ("app", "semantic_memory_refresh_interval_seconds"): (
        "Seconds between schema refresh scans for each enabled connection."
    ),
    ("app", "semantic_memory_refresh_max_tables"): (
        "Hard per-connection table bound for one schema refresh scan."
    ),
}

_SENSITIVE_FIELDS = {
    ("app", "api_keys"),
    ("app", "mcp_api_keys"),
    ("app", "vault_token"),
    ("app", "concurrency_redis_url"),
    ("connection", "connection_string"),
}

_ERRORS: dict[str, tuple[str, str, list[str], str]] = {
    "NOT_FOUND": (
        "Resource unavailable",
        "The requested resource does not exist or is not visible under the caller's effective policy. QueryGate deliberately does not distinguish those cases.",
        ["List visible connections or tables and retry using an identifier from that result."],
        "troubleshooting.errors",
    ),
    "RESOURCE_UNAVAILABLE": (
        "Resource unavailable",
        "The connection, table, or column is missing or unavailable under the caller's effective policy.",
        [
            "Use the discovery tools to choose a visible resource.",
            "Ask an administrator to review policy if access is expected.",
        ],
        "troubleshooting.errors",
    ),
    "VALIDATION": (
        "Request validation failed",
        "The structured request or referenced schema does not satisfy QueryGate's input and policy rules.",
        [
            "Read the returned safe validation message.",
            "Describe the table and submit a narrower structured request.",
        ],
        "troubleshooting.errors",
    ),
    "POLICY_LIMIT_EXCEEDED": (
        "Policy guardrail exceeded",
        "The request exceeds an effective row, join, complexity, timeout, concurrency, or response-size guardrail.",
        [
            "Reduce the request scope.",
            "Inspect your access summary or ask an administrator about the effective policy.",
        ],
        "configuration.policy",
    ),
    "CONCURRENCY_LIMIT": (
        "Concurrency capacity unavailable",
        "The connection's configured concurrency capacity could not be acquired within its wait period.",
        ["Retry once after a short delay.", "Do not retry in a tight loop."],
        "troubleshooting.errors",
    ),
    "SCOPE_REQUIRED": (
        "Additional scope required",
        "The authenticated principal does not hold the scope required for this operation.",
        [
            "Review your access summary.",
            "Ask an administrator for the minimum required scope; do not broaden unrelated access.",
        ],
        "authentication.scopes",
    ),
    "FORBIDDEN": (
        "Additional scope required",
        "The authenticated principal does not hold the scope required for this operation.",
        [
            "Review your access summary.",
            "Ask an administrator for the minimum required scope; do not broaden unrelated access.",
        ],
        "authentication.scopes",
    ),
    "CONFIG_INVALID": (
        "Configuration validation failed",
        "A proposed configuration does not satisfy QueryGate's model, reference, or cross-file validation rules.",
        [
            "Correct every reported validation error before staging.",
            "Run querygate-validate-config in CI.",
        ],
        "configuration.governance",
    ),
    "INTERNAL": (
        "Internal error",
        "QueryGate masked an unexpected internal failure because database drivers and raw exceptions can contain sensitive deployment details.",
        [
            "Give the operator the X-Request-ID and timestamp.",
            "Inspect structured server logs rather than asking the caller for secrets.",
        ],
        "troubleshooting.errors",
    ),
}


class GuideService:
    def __init__(self, corpus: Optional[GuideCorpus] = None) -> None:
        self._corpus = corpus or get_guide_corpus()

    @property
    def version(self) -> str:
        return self._corpus.version

    def _citation(self, topic: GuideTopic) -> GuideCitation:
        return GuideCitation(source=topic.source, topic_id=topic.id, version=self.version)

    def search(self, query: str, limit: int = 5) -> GuideSearchResponse:
        hits = [
            GuideSearchHit(
                topic_id=topic.id,
                title=topic.title,
                summary=topic.summary,
                score=score,
                citation=self._citation(topic),
                next_actions=topic.next_actions,
            )
            for topic, score in self._corpus.search(query, limit=limit)
        ]
        return GuideSearchResponse(
            querygate_version=self.version, query=query.strip(), results=hits
        )

    def topic(self, topic_id: str, *, max_response_bytes: int = 16_384) -> GuideTopicResponse:
        if max_response_bytes < 512:
            raise ValueError("guide topic max_response_bytes must be at least 512")
        topic = self._corpus.get(topic_id)
        response = GuideTopicResponse(
            querygate_version=self.version,
            topic_id=topic.id,
            title=topic.title,
            summary=topic.summary,
            content=topic.body,
            citation=self._citation(topic),
            next_actions=topic.next_actions,
            max_response_bytes=max_response_bytes,
        )
        full_bytes = len(json.dumps(response.model_dump(mode="json")).encode("utf-8"))
        if full_bytes <= max_response_bytes:
            return response
        # Measure the envelope with empty content to find how much of the
        # budget is left for the body text, then truncate to fit exactly —
        # same incremental-measurement approach as search_catalog's
        # max_response_bytes (catalog/retrieval.py).
        envelope = response.model_copy(update={"content": "", "truncated": True})
        envelope_bytes = len(json.dumps(envelope.model_dump(mode="json")).encode("utf-8"))
        budget_for_content = max(max_response_bytes - envelope_bytes, 0)
        truncated_content = topic.body.encode("utf-8")[:budget_for_content].decode(
            "utf-8", errors="ignore"
        )
        return envelope.model_copy(update={"content": truncated_content})

    def setup_checklist(
        self, profile: Literal["local", "container", "production"] = "local"
    ) -> SetupChecklistResponse:
        common = [
            (
                "Choose configuration files",
                "Copy and edit the bundled connections, policy, and optional catalog examples.",
                "configuration.connections",
            ),
            (
                "Validate configuration",
                "Run querygate-validate-config before starting or reloading QueryGate.",
                "configuration.governance",
            ),
            (
                "Start QueryGate",
                "Start the service and confirm /health reports the expected version and readiness state.",
                "setup.first-run",
            ),
            (
                "Discover safely",
                "List visible connections and describe tables before issuing a structured query.",
                "interfaces.mcp-rest",
            ),
        ]
        additions = {
            "local": [],
            "container": [
                (
                    "Mount configuration and secrets",
                    "Mount configuration read-only and inject credential values through the container secret mechanism.",
                    "configuration.connections",
                ),
            ],
            "production": [
                (
                    "Configure authentication",
                    "Require API keys or JWT, grant minimum scopes, and disable anonymous development access.",
                    "authentication.scopes",
                ),
                (
                    "Configure distributed guardrails",
                    "Use Redis concurrency enforcement when more than one QueryGate instance serves the same database.",
                    "operations.observability",
                ),
                (
                    "Exercise governance and rollback",
                    "Preview, stage, apply, and roll back a harmless configuration change before production traffic.",
                    "configuration.governance",
                ),
            ],
        }[profile]
        steps = []
        for number, (title, instruction, topic_id) in enumerate(common + additions, start=1):
            topic = self._corpus.get(topic_id)
            steps.append(
                SetupStep(
                    number=number,
                    title=title,
                    instruction=instruction,
                    citation=self._citation(topic),
                )
            )
        return SetupChecklistResponse(querygate_version=self.version, profile=profile, steps=steps)

    def explain_config_field(self, model: str, field: str) -> ConfigFieldExplanation:
        model_key = model.strip().lower()
        field_key = field.strip()
        model_type = _MODEL_ALIASES.get(model_key)
        if model_type is None:
            raise NotFoundError(f"Unknown configuration model: {model!r}")
        field_info = model_type.model_fields.get(field_key)
        if field_info is None:
            raise NotFoundError(f"Unknown configuration field: {model_key}.{field_key}")

        default: object = None
        required = field_info.is_required()
        if not required and field_info.default is not PydanticUndefined:
            default = field_info.default
        if (model_key, field_key) in _SENSITIVE_FIELDS:
            default = "[redacted]" if not required else None
        description = (
            _FIELD_DESCRIPTIONS.get((model_key, field_key))
            or field_info.description
            or f"Configuration field {model_key}.{field_key}."
        )
        topic = self._corpus.get(_MODEL_TOPICS[model_key])
        return ConfigFieldExplanation(
            querygate_version=self.version,
            model=model_key,
            field=field_key,
            type=_annotation_name(field_info.annotation),
            required=required,
            default=default,
            description=description,
            citation=self._citation(topic),
        )

    def access_summary(self, principal: Principal) -> AccessSummary:
        connections = list_visible_connections(principal)
        scopes = sorted(principal.scopes)
        topic = self._corpus.get("authentication.scopes")
        return AccessSummary(
            querygate_version=self.version,
            principal=principal.subject,
            auth_method=principal.auth_method,
            scopes=scopes,
            capabilities=CallerCapabilities(
                query_visible_connections=bool(connections),
                read_configuration=ADMIN_CONFIG_READ_SCOPE in principal.scopes,
                change_configuration=ADMIN_CONFIG_WRITE_SCOPE in principal.scopes,
                reload_configuration=ADMIN_RELOAD_CONFIG_SCOPE in principal.scopes,
            ),
            visible_connections=connections,
            connection_access=_connection_access_details(principal),
            guidance=(
                "Use only the connections returned here. A resource that is absent may not "
                "exist or may not be visible under your effective policy."
            ),
            citation=self._citation(topic),
        )

    def explain_error(self, error_code: str) -> ErrorExplanation:
        code = error_code.strip().upper()
        entry = _ERRORS.get(code)
        if entry is None:
            raise NotFoundError(f"Unknown QueryGate error code: {error_code!r}")
        title, explanation, next_actions, topic_id = entry
        topic = self._corpus.get(topic_id)
        return ErrorExplanation(
            querygate_version=self.version,
            error_code=code,
            title=title,
            explanation=explanation,
            next_actions=next_actions,
            citation=self._citation(topic),
        )

    def redacted_configuration(self, cfg: AppConfig, principal: Principal) -> RedactedConfiguration:
        if ADMIN_CONFIG_READ_SCOPE not in principal.scopes:
            raise AuthorizationError(ADMIN_CONFIG_READ_SCOPE)
        version = governance.get_current(cfg)
        connections_raw = _mapping(yaml.safe_load(version.connections_yaml)).get("connections", [])
        connections = [_redacted_connection(entry) for entry in connections_raw]

        policy_raw = _mapping(yaml.safe_load(version.policy_yaml))
        default_raw = _mapping(policy_raw.get("default"))
        default_policy = Policy.model_validate(default_raw)
        overrides: dict[str, RedactedPolicySummary] = {}
        for connection_id, override in _mapping(policy_raw.get("connections")).items():
            resolved = Policy.model_validate({**default_raw, **_mapping(override)})
            overrides[str(connection_id)] = _policy_summary(resolved)

        catalog = _redacted_catalog(version.catalog_yaml)
        topic = self._corpus.get("configuration.governance")
        return RedactedConfiguration(
            querygate_version=self.version,
            version=ConfigVersionSummary(
                id=version.id,
                status=version.status.value,
                created_at=version.created_at.isoformat(),
                created_by=_redacted_actor(version.created_by),
                description=None,
                applied_at=version.applied_at.isoformat() if version.applied_at else None,
                applied_by=_redacted_actor(version.applied_by) if version.applied_by else None,
                previous_active_version_id=version.previous_active_version_id,
            ),
            connections=sorted(connections, key=lambda item: item.id),
            policy=RedactedPolicyConfiguration(
                default=_policy_summary(default_policy),
                connection_overrides=dict(sorted(overrides.items())),
                principal_override_count=len(_mapping(policy_raw.get("principals"))),
            ),
            catalog=catalog,
            redactions=[
                "connection strings and credential values/references",
                "table and column policy identifiers",
                "mandatory row-filter values and claim names",
                "principal override subjects and contents",
                "catalog identifiers and descriptions",
                "free-form connection and version descriptions",
            ],
            citation=self._citation(topic),
        )


def _connection_access_details(principal: Optional[Principal]) -> list[ConnectionAccessDetail]:
    """Effective guardrails and mandatory-filter claim readiness per visible
    connection, for the item 45 "my access" portal.

    Mirrors item 39's `simulate_candidate_policy` redaction posture (never a
    filter/claim *value*, only table/column/source/readiness) but against the
    caller's own already-active policy rather than an uncommitted candidate,
    and across every mandatory filter on the connection rather than a single
    requested table.
    """
    details: list[ConnectionAccessDetail] = []
    for info in list_visible_connections(principal):
        _, policy = resolve_visible_connection(info.id, principal=principal)
        guardrails = EffectiveGuardrails.model_validate(
            policy.model_dump(include=set(EffectiveGuardrails.model_fields))
        )
        filters: list[MandatoryFilterReadiness] = []
        for row_filter in policy.mandatory_row_filters:
            if not policy.table_allowed(row_filter.table):
                continue
            ready = True
            if row_filter.from_claim is not None:
                try:
                    row_filter.resolve(principal)
                except PolicyViolationError:
                    ready = False
            filters.append(
                MandatoryFilterReadiness(
                    table=row_filter.table,
                    column=row_filter.column,
                    source="claim" if row_filter.from_claim is not None else "configured_literal",
                    claim=row_filter.from_claim,
                    ready=ready,
                )
            )
        details.append(
            ConnectionAccessDetail(
                connection=info.id, guardrails=guardrails, mandatory_filters=filters
            )
        )
    return details


def _annotation_name(annotation: Any) -> str:
    return str(annotation).replace("typing.", "").replace("<class '", "").replace("'>", "")


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _credential_source(value: object) -> str:
    if not isinstance(value, str):
        return "configured"
    match = _REFERENCE_RE.match(value.strip())
    if match is None:
        return "configured"
    return "external_secret" if ":" in match.group(1) else "environment"


def _redacted_actor(subject: str) -> str:
    return "system" if subject.startswith("system:") else "authenticated-principal"


def _redacted_connection(raw: object) -> RedactedConnection:
    entry = _mapping(raw)
    profile = ConnectionProfile.model_validate(entry)
    return RedactedConnection(
        id=profile.id,
        dialect=profile.dialect,
        enabled=profile.enabled,
        description=None,
        credential_source=_credential_source(entry.get("connection_string")),
        known_table_count=len(profile.known_tables),
        join_group_configured=profile.join_group is not None,
    )


_GUARDRAIL_FIELDS = (
    "max_joins",
    "max_select_columns",
    "max_where_depth",
    "max_group_by",
    "max_limit",
    "max_limit_aggregate",
    "default_limit",
    "max_top_n",
    "max_partition_by",
    "max_batch_size",
    "max_response_bytes",
    "timeout_seconds",
    "max_concurrency",
    "concurrency_wait_seconds",
    "max_queue_depth",
    "max_queue_depth_per_principal",
    "log_query_literals",
    "max_estimated_rows",
    "max_estimated_cost",
    "cost_estimation_mode",
)


def _policy_summary(policy: Policy) -> RedactedPolicySummary:
    return RedactedPolicySummary(
        enabled=policy.enabled,
        rule_counts=PolicyRuleCounts(
            allowed_tables=len(policy.allowed_tables),
            denied_tables=len(policy.denied_tables),
            allowed_column_groups=len(policy.allowed_columns),
            denied_column_groups=len(policy.denied_columns),
            mandatory_row_filters=len(policy.mandatory_row_filters),
        ),
        guardrails={name: getattr(policy, name) for name in _GUARDRAIL_FIELDS},
        join_group_configured=policy.join_group is not None,
    )


def _redacted_catalog(catalog_yaml: Optional[str]) -> RedactedCatalogSummary:
    if catalog_yaml is None:
        return RedactedCatalogSummary(configured=False)
    catalog = SchemaCatalog.model_validate(_mapping(yaml.safe_load(catalog_yaml)))
    table_count = sum(len(connection.tables) for connection in catalog.connections.values())
    column_count = sum(
        len(table.columns)
        for connection in catalog.connections.values()
        for table in connection.tables.values()
    )
    return RedactedCatalogSummary(
        configured=True,
        version=catalog.version,
        connection_count=len(catalog.connections),
        table_count=table_count,
        column_count=column_count,
    )


@lru_cache(maxsize=1)
def get_guide_service() -> GuideService:
    service = GuideService()
    if service.version != __version__:
        raise ValueError(
            f"Packaged guide version {service.version!r} does not match QueryGate {__version__!r}."
        )
    return service
