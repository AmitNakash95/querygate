"""Orchestrates config-governance validate/stage/apply for the admin API.

Layered on top of the same primitives item 5 (config_reload) and item 17
(cli.validate_config) already built — applying a version is exactly
`reload_config()` pointed at that version's files, and validating a
candidate is exactly `validate_config()` pointed at a scratch directory.
Neither of those gets a parallel implementation here.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from querygate.admin.access_diff import compute_access_diff
from querygate.admin.models import (
    CandidateColumnDecision,
    CandidatePolicySimulation,
    CandidatePolicySimulationRequest,
    CandidateSimulationReason,
    ConfigDocumentPreview,
    ConfigPreview,
    ConfigSemanticDiffRequest,
    ConfigVersion,
    ConfigVersionStatus,
    EffectiveGuardrails,
    MandatoryFilterReadiness,
    SemanticAccessDiff,
)
from querygate.admin.store import ConfigVersionStore, get_config_version_store
from querygate.audit.logger import audit_config_change
from querygate.cli import LoadedConfigContext, load_config_context, validate_config
from querygate.config_reload import ReloadResult, reload_config
from querygate.connections.models import ConnectionProfile
from querygate.connections.visibility import resolve_visible_connection_from
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import (
    ConfigValidationError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
)
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE
from querygate.policy.models import Policy
from querygate.secrets.resolvers import build_secret_resolver_registry
from querygate.validation.policy_validation import referenced_tables, validate_policy
from querygate.validation.schema_validation import resolve_query_table_connections

_SAFE_SIMULATION_VALIDATION_ERROR = (
    "Candidate configuration is invalid; run the config validation endpoint for details "
    "before simulating it."
)


def _bootstrap(cfg: AppConfig, store: ConfigVersionStore) -> ConfigVersion:
    connections_yaml = Path(cfg.connections_file).read_text()
    policy_yaml = Path(cfg.policy_file).read_text()
    catalog_yaml = (
        Path(cfg.catalog_file).read_text()
        if cfg.catalog_file and Path(cfg.catalog_file).exists()
        else None
    )
    return store.bootstrap_if_empty(
        connections_yaml=connections_yaml, policy_yaml=policy_yaml, catalog_yaml=catalog_yaml
    )


def _resolve_candidate(
    cfg: AppConfig,
    store: ConfigVersionStore,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    """A field left unset (None) inherits unchanged from the active version.

    There is no dedicated way to explicitly clear a catalog back to "none"
    through this API — pass an empty-but-present catalog document (e.g.
    `"connections: {}"`) if that's genuinely needed; this keeps the request
    shape simple (no separate "unset" sentinel) for what is, in practice, a
    rare edge case.
    """
    active = _bootstrap(cfg, store)
    return (
        connections_yaml if connections_yaml is not None else active.connections_yaml,
        policy_yaml if policy_yaml is not None else active.policy_yaml,
        catalog_yaml if catalog_yaml is not None else active.catalog_yaml,
    )


def _resolve_candidate_without_persisting(
    cfg: AppConfig,
    store: ConfigVersionStore,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    """Resolve inheritance without bootstrapping config-governance history."""
    active = store.get_active_version()
    if active is None:
        active_connections = Path(cfg.connections_file).read_text()
        active_policy = Path(cfg.policy_file).read_text()
        active_catalog = (
            Path(cfg.catalog_file).read_text()
            if cfg.catalog_file and Path(cfg.catalog_file).exists()
            else None
        )
    else:
        active_connections = active.connections_yaml
        active_policy = active.policy_yaml
        active_catalog = active.catalog_yaml
    return (
        connections_yaml if connections_yaml is not None else active_connections,
        policy_yaml if policy_yaml is not None else active_policy,
        catalog_yaml if catalog_yaml is not None else active_catalog,
    )


def _load_isolated_candidate_context(
    cfg: AppConfig,
    connections_yaml: str,
    policy_yaml: str,
    catalog_yaml: Optional[str],
) -> LoadedConfigContext:
    """Use the normal loaders/cross-file checks without installing globals."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        connections_file = tmp_path / "connections.yaml"
        connections_file.write_text(connections_yaml)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(policy_yaml)
        catalog_file: Optional[str] = None
        if catalog_yaml is not None:
            catalog_path = tmp_path / "catalog.yaml"
            catalog_path.write_text(catalog_yaml)
            catalog_file = str(catalog_path)
        context, errors = load_config_context(
            str(connections_file),
            str(policy_file),
            catalog_file,
            resolver_registry=build_secret_resolver_registry(cfg),
        )
    if errors or context is None:
        # Validation details can echo secret-reference names, static filter
        # input, or other candidate content. The existing /validate endpoint
        # is the intentionally detailed surface; simulation stays redacted.
        raise ConfigValidationError(_SAFE_SIMULATION_VALIDATION_ERROR)
    return context


def _effective_guardrails(policy: Policy) -> EffectiveGuardrails:
    return EffectiveGuardrails.model_validate(
        policy.model_dump(include=set(EffectiveGuardrails.model_fields))
    )


def simulate_candidate_policy(
    cfg: AppConfig,
    actor: Principal,
    request: CandidatePolicySimulationRequest,
) -> CandidatePolicySimulation:
    """Evaluate an uncommitted candidate without mutating live config state."""
    start = time.monotonic()
    store = get_config_version_store()
    try:
        resolved_connections, resolved_policy, resolved_catalog = (
            _resolve_candidate_without_persisting(
                cfg,
                store,
                connections_yaml=request.connections_yaml,
                policy_yaml=request.policy_yaml,
                catalog_yaml=request.catalog_yaml,
            )
        )
        context = _load_isolated_candidate_context(
            cfg, resolved_connections, resolved_policy, resolved_catalog
        )
    except ConfigValidationError:
        audit_config_change(
            action="simulate",
            outcome="rejected",
            principal=actor.subject,
            principal_scopes=sorted(actor.scopes),
            auth_method=actor.auth_method,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise

    target = Principal(
        subject=request.principal,
        claims=request.claims,
        auth_method="admin_candidate_simulation",
    )
    reasons: list[CandidateSimulationReason] = []

    def candidate_resolver(
        connection_id: str, principal: Optional[Principal]
    ) -> Tuple[ConnectionProfile, Policy]:
        return resolve_visible_connection_from(
            context.registry,
            context.policy_store,
            connection_id,
            principal=principal,
        )

    try:
        _profile, policy = candidate_resolver(request.connection, target)
    except NotFoundError:
        result = CandidatePolicySimulation(
            decision="deny",
            principal=request.principal,
            connection=request.connection,
            table=request.table,
            query_evaluated=request.query is not None,
            query_allowed=False if request.query is not None else None,
            reasons=[
                CandidateSimulationReason(
                    code="connection_not_visible",
                    message=(
                        "The target connection is not visible to this principal under the "
                        "candidate configuration."
                    ),
                )
            ],
        )
        audit_config_change(
            action="simulate",
            outcome="success",
            principal=actor.subject,
            principal_scopes=sorted(actor.scopes),
            auth_method=actor.auth_method,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return result

    column_decisions: list[CandidateColumnDecision] = []
    requested_table_allowed = (
        policy.table_allowed(request.table) if request.table is not None else True
    )
    if not requested_table_allowed:
        reasons.append(
            CandidateSimulationReason(
                code="table_denied",
                message="The requested table is denied by the effective candidate policy.",
            )
        )
    for column in request.columns:
        allowed = requested_table_allowed and policy.column_allowed(request.table or "", column)
        column_decisions.append(CandidateColumnDecision(column=column, allowed=allowed))
        if not allowed:
            reasons.append(
                CandidateSimulationReason(
                    code="column_denied",
                    message=("A requested column is denied by the effective candidate policy."),
                )
            )

    query_allowed: Optional[bool] = None
    requested_tables = {request.table} if request.table is not None else set()
    if request.query is not None:
        requested_tables.update(referenced_tables(request.query))
        query_allowed = True
        try:
            validate_policy(request.query, policy, connection_id=request.connection)
        except PolicyViolationError as exc:
            query_allowed = False
            reasons.append(CandidateSimulationReason(code="query_policy_denied", message=str(exc)))
        if query_allowed:
            try:
                resolve_query_table_connections(
                    request.query,
                    request.connection,
                    principal=target,
                    connection_resolver=candidate_resolver,
                )
            except (NotFoundError, QueryValidationError):
                query_allowed = False
                reasons.append(
                    CandidateSimulationReason(
                        code="query_connection_denied",
                        message=(
                            "The structured query references a connection that is not visible "
                            "to the target principal or is outside the candidate join group."
                        ),
                    )
                )

    # Do not enumerate mandatory-filter identifiers for a table the target
    # policy itself hides. Requested names may still receive a table-denied
    # decision, but no additional hidden policy metadata rides with it.
    requested_table_keys = {
        table.casefold() for table in requested_tables if policy.table_allowed(table)
    }
    filter_readiness: list[MandatoryFilterReadiness] = []
    for row_filter in policy.mandatory_row_filters:
        if row_filter.table.casefold() not in requested_table_keys:
            continue
        ready = True
        if row_filter.from_claim is not None:
            try:
                row_filter.resolve(target)
            except PolicyViolationError:
                ready = False
                reasons.append(
                    CandidateSimulationReason(
                        code="mandatory_claim_missing",
                        message="A mandatory row-filter claim is missing for the target principal.",
                    )
                )
        filter_readiness.append(
            MandatoryFilterReadiness(
                table=row_filter.table,
                column=row_filter.column,
                source=("claim" if row_filter.from_claim is not None else "configured_literal"),
                claim=row_filter.from_claim,
                ready=ready,
            )
        )

    if not reasons:
        reasons.append(
            CandidateSimulationReason(
                code="allowed",
                message="The candidate policy permits the requested access shape.",
            )
        )
    decision = "allow" if all(reason.code == "allowed" for reason in reasons) else "deny"
    result = CandidatePolicySimulation(
        decision=decision,
        principal=request.principal,
        connection=request.connection,
        table=request.table,
        columns=column_decisions,
        query_evaluated=request.query is not None,
        query_allowed=query_allowed,
        mandatory_filters=filter_readiness,
        guardrails=_effective_guardrails(policy),
        reasons=reasons,
    )
    audit_config_change(
        action="simulate",
        outcome="success",
        principal=actor.subject,
        principal_scopes=sorted(actor.scopes),
        auth_method=actor.auth_method,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return result


def diff_candidate_access(
    cfg: AppConfig,
    actor: Principal,
    request: ConfigSemanticDiffRequest,
) -> SemanticAccessDiff:
    """Diff *resolved* access between the active version and an uncommitted
    candidate, without mutating live config state.

    Both snapshots are loaded through the same isolated candidate-context path
    item 39 uses for simulation, so the live registry/policy/catalog singletons
    and any concurrent request are provably untouched. Like `/simulate`, this
    resolves caller-supplied config/secret references (write-like) while
    echoing back semantic policy detail (read-like), so the route requires both
    config scopes.
    """
    start = time.monotonic()
    store = get_config_version_store()
    try:
        active_connections, active_policy, active_catalog = _resolve_candidate_without_persisting(
            cfg, store, connections_yaml=None, policy_yaml=None, catalog_yaml=None
        )
        candidate_connections, candidate_policy, candidate_catalog = (
            _resolve_candidate_without_persisting(
                cfg,
                store,
                connections_yaml=request.connections_yaml,
                policy_yaml=request.policy_yaml,
                catalog_yaml=request.catalog_yaml,
            )
        )
        active_context = _load_isolated_candidate_context(
            cfg, active_connections, active_policy, active_catalog
        )
        candidate_context = _load_isolated_candidate_context(
            cfg, candidate_connections, candidate_policy, candidate_catalog
        )
    except ConfigValidationError:
        audit_config_change(
            action="diff",
            outcome="rejected",
            principal=actor.subject,
            principal_scopes=sorted(actor.scopes),
            auth_method=actor.auth_method,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise

    result = compute_access_diff(active_context, candidate_context)
    audit_config_change(
        action="diff",
        outcome="success",
        principal=actor.subject,
        principal_scopes=sorted(actor.scopes),
        auth_method=actor.auth_method,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return result


def validate_candidate_content(
    cfg: AppConfig, connections_yaml: str, policy_yaml: str, catalog_yaml: Optional[str]
) -> List[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        connections_file = tmp_path / "connections.yaml"
        connections_file.write_text(connections_yaml)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(policy_yaml)
        catalog_file: Optional[str] = None
        if catalog_yaml is not None:
            catalog_path = tmp_path / "catalog.yaml"
            catalog_path.write_text(catalog_yaml)
            catalog_file = str(catalog_path)
        return validate_config(
            str(connections_file),
            str(policy_file),
            catalog_file,
            resolver_registry=build_secret_resolver_registry(cfg),
        )


def validate(
    cfg: AppConfig,
    principal: Optional[Principal] = None,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> List[str]:
    start = time.monotonic()
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog = _resolve_candidate(
        cfg,
        store,
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog
    )
    if principal is not None:
        audit_config_change(
            action="validate",
            outcome="rejected" if errors else "success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            error_category="validation" if errors else None,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    return errors


def preview(
    cfg: AppConfig,
    principal: Principal,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> ConfigPreview:
    """Validate a candidate and return a content-free document-level preview.

    A caller with config-write but not config-read must be able to validate a
    proposal without using preview as a side channel into the current files,
    so this deliberately returns no paths, values, identifiers, hashes, or
    line-level differences.
    """
    start = time.monotonic()
    store = get_config_version_store()
    active = _bootstrap(cfg, store)
    resolved_connections, resolved_policy, resolved_catalog = _resolve_candidate(
        cfg,
        store,
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog
    )
    can_compare = ADMIN_CONFIG_READ_SCOPE in principal.scopes

    def change(
        submitted: Optional[str], resolved: Optional[str], active_value: Optional[str]
    ) -> str:
        if not can_compare:
            return "submitted" if submitted is not None else "inherited"
        return "changed" if resolved != active_value else "unchanged"

    documents = [
        ConfigDocumentPreview(
            document="connections",
            change=change(connections_yaml, resolved_connections, active.connections_yaml),
        ),
        ConfigDocumentPreview(
            document="policy",
            change=change(policy_yaml, resolved_policy, active.policy_yaml),
        ),
        ConfigDocumentPreview(
            document="catalog",
            change=change(catalog_yaml, resolved_catalog, active.catalog_yaml),
        ),
    ]
    audit_config_change(
        action="preview",
        outcome="rejected" if errors else "success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        error_category="validation" if errors else None,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return ConfigPreview(
        valid=not errors,
        errors=errors,
        documents=documents,
        ready_to_stage=not errors
        and any(item.change in ("changed", "submitted") for item in documents),
    )


def stage(
    cfg: AppConfig,
    principal: Principal,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
    description: Optional[str],
) -> ConfigVersion:
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog = _resolve_candidate(
        cfg,
        store,
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog
    )
    if errors:
        audit_config_change(
            action="stage",
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            description=description,
            error_category="validation",
        )
        raise ConfigValidationError("; ".join(errors))

    version = store.create_staged_version(
        connections_yaml=resolved_connections,
        policy_yaml=resolved_policy,
        catalog_yaml=resolved_catalog,
        description=description,
        actor=principal.subject,
    )
    audit_config_change(
        action="stage",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=version.id,
        description=description,
    )
    return version


async def apply(
    cfg: AppConfig, principal: Principal, version_id: str
) -> Tuple[ConfigVersion, ReloadResult]:
    """Activate `version_id` — this is "apply" for a staged version and
    "rollback" for a version that was previously active (same operation;
    the audit action label is chosen from the version's status just before
    it's mutated).
    """
    store = get_config_version_store()
    version = store.get_version(version_id)  # raises NotFoundError -> 404 at the route
    action = "rollback" if version.status == ConfigVersionStatus.INACTIVE else "apply"
    start = time.monotonic()

    errors = validate_candidate_content(
        cfg, version.connections_yaml, version.policy_yaml, version.catalog_yaml
    )
    if errors:
        audit_config_change(
            action=action,
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            version_id=version_id,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise ConfigValidationError("; ".join(errors))

    paths = store.file_paths(version_id)
    reload_result = await reload_config(
        connections_file=paths.connections,
        policy_file=paths.policy,
        catalog_file=paths.catalog,
        resolver_registry=build_secret_resolver_registry(cfg),
    )
    updated_version = store.mark_active(version_id, actor=principal.subject)
    audit_config_change(
        action=action,
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=version_id,
        previous_version_id=updated_version.previous_active_version_id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return updated_version, reload_result


def get_current(cfg: AppConfig) -> ConfigVersion:
    store = get_config_version_store()
    return _bootstrap(cfg, store)


def list_versions(cfg: AppConfig) -> List[ConfigVersion]:
    store = get_config_version_store()
    _bootstrap(cfg, store)
    return store.list_versions()


def get_version(version_id: str) -> ConfigVersion:
    return get_config_version_store().get_version(version_id)
