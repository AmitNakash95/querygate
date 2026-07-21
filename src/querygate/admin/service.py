"""Orchestrates config-governance validate/stage/apply for the admin API.

Layered on top of the same primitives item 5 (config_reload) and item 17
(cli.validate_config) already built — applying a version is exactly
`reload_config()` pointed at that version's files, and validating a
candidate is exactly `validate_config()` pointed at a scratch directory.
Neither of those gets a parallel implementation here.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

import sqlalchemy as sa
import yaml

from querygate.admin.access_diff import compute_access_diff
from querygate.admin.blast_radius import compute_blast_radius_report
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
    PolicyBlastRadiusReport,
    SemanticAccessDiff,
    TemplateSchemaCheck,
    TemplateSchemaCheckResult,
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
from querygate.templates.binding import dummy_bound_query
from querygate.templates.loader import TemplateStore
from querygate.templates.models import QueryTemplate
from querygate.validation.policy_validation import referenced_tables, validate_policy
from querygate.validation.schema_validation import (
    resolve_query_table_connections,
    validate_schema,
)

_SAFE_SIMULATION_VALIDATION_ERROR = (
    "Candidate configuration is invalid; run the config validation endpoint for details "
    "before simulating it."
)


def _read_optional_file(path: Optional[str]) -> Optional[str]:
    return Path(path).read_text() if path and Path(path).exists() else None


def _bootstrap(cfg: AppConfig, store: ConfigVersionStore) -> ConfigVersion:
    connections_yaml = Path(cfg.connections_file).read_text()
    policy_yaml = Path(cfg.policy_file).read_text()
    catalog_yaml = _read_optional_file(cfg.catalog_file)
    templates_yaml = _read_optional_file(cfg.template_file)
    return store.bootstrap_if_empty(
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
        templates_yaml=templates_yaml,
    )


def _resolve_candidate(
    cfg: AppConfig,
    store: ConfigVersionStore,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
    templates_yaml: Optional[str],
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """A field left unset (None) inherits unchanged from the active version.

    There is no dedicated way to explicitly clear a catalog or templates
    document back to "none" through this API — pass an empty-but-present
    document (e.g. `"templates: []"`) if that's genuinely needed; this keeps
    the request shape simple (no separate "unset" sentinel) for what is, in
    practice, a rare edge case.
    """
    active = _bootstrap(cfg, store)
    return (
        connections_yaml if connections_yaml is not None else active.connections_yaml,
        policy_yaml if policy_yaml is not None else active.policy_yaml,
        catalog_yaml if catalog_yaml is not None else active.catalog_yaml,
        templates_yaml if templates_yaml is not None else active.templates_yaml,
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


def compute_blast_radius(
    cfg: AppConfig,
    actor: Principal,
    request: ConfigSemanticDiffRequest,
) -> PolicyBlastRadiusReport:
    """Aggregate the resolved-access diff across the connection baseline and
    every explicitly configured principal, without mutating live config state.

    Shares `/diff`'s isolated-context loading and scope reasoning exactly —
    see `diff_candidate_access` — and additionally re-resolves policy once per
    configured principal (bounded — see `admin/blast_radius.py`) using the
    same `compute_access_diff` primitive, never a parallel resolution path.
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
            action="blast_radius",
            outcome="rejected",
            principal=actor.subject,
            principal_scopes=sorted(actor.scopes),
            auth_method=actor.auth_method,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise

    result = compute_blast_radius_report(active_context, candidate_context)
    audit_config_change(
        action="blast_radius",
        outcome="success",
        principal=actor.subject,
        principal_scopes=sorted(actor.scopes),
        auth_method=actor.auth_method,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return result


_PYDANTIC_URL_LINE = re.compile(r"\n\s*For further information visit https://\S+")
# The trailing "[type=..., input_value=..., input_type=...]" noise pydantic
# appends to each error line — useful for library debugging, not for an admin
# reading a config dry-run.
_PYDANTIC_TAIL = re.compile(r"\s*\[type=[^\n]*?input_type=[^\]\n]*\]")


def _humanize_validation_errors(errors: List[str], doc_names: dict) -> List[str]:
    """Turn raw validate_config errors into something an admin can act on in the
    dry-run panel: attribute each to its logical document name (never the
    throwaway temp path the candidate was written to) and strip pydantic's
    library-debugging boilerplate (the docs URL line and the
    `[type=..., input_value=..., input_type=...]` tail). The underlying human
    message and field path (e.g. `templates.0.parameters.0`) are preserved."""
    cleaned: List[str] = []
    for error in errors:
        for path, name in doc_names.items():
            error = error.replace(path, name)
        error = _PYDANTIC_URL_LINE.sub("", error)
        error = _PYDANTIC_TAIL.sub("", error)
        cleaned.append(error.strip())
    return cleaned


def validate_candidate_content(
    cfg: AppConfig,
    connections_yaml: str,
    policy_yaml: str,
    catalog_yaml: Optional[str],
    templates_yaml: Optional[str] = None,
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
        template_file: Optional[str] = None
        if templates_yaml is not None:
            templates_path = tmp_path / "templates.yaml"
            templates_path.write_text(templates_yaml)
            template_file = str(templates_path)
        errors = validate_config(
            str(connections_file),
            str(policy_file),
            catalog_file,
            template_file=template_file,
            resolver_registry=build_secret_resolver_registry(cfg),
        )
        doc_names = {
            str(connections_file): "connections.yaml",
            str(policy_file): "policy.yaml",
        }
        if catalog_file is not None:
            doc_names[catalog_file] = "catalog.yaml"
        if template_file is not None:
            doc_names[template_file] = "templates.yaml"
        return _humanize_validation_errors(errors, doc_names)


async def _check_one_template_schema(template: QueryTemplate) -> TemplateSchemaCheck:
    """Reflect the target connection and verify the template's referenced
    tables/columns exist. Reuses the exact `validate_schema` the live pipeline
    runs (with `principal=None`, so the check resolves the connection by its
    deployment visibility, not a query-time principal policy) against a
    dummy-bound query — placeholder values never change which identifiers a
    query references."""
    base = {"template_id": template.id, "connection": template.connection}
    try:
        bound = dummy_bound_query(template)
    except Exception as exc:  # structurally invalid skeleton — fix in the dry-run
        return TemplateSchemaCheck(**base, status="structural_error", messages=[str(exc)])
    try:
        await validate_schema(bound, template.connection, principal=None)
    except NotFoundError:
        return TemplateSchemaCheck(
            **base,
            status="connection_unavailable",
            messages=[
                f"connection {template.connection!r} is not a live, enabled connection "
                "to reflect a schema from"
            ],
        )
    except QueryValidationError as exc:  # a missing column, or a structural rule
        return TemplateSchemaCheck(**base, status="issues", messages=[str(exc)])
    except sa.exc.NoSuchTableError as exc:  # a referenced table doesn't exist
        return TemplateSchemaCheck(
            **base, status="issues", messages=[f"table {str(exc)!r} does not exist"]
        )
    except sa.exc.SQLAlchemyError:  # DB unreachable/auth/etc — best-effort, never blocks
        return TemplateSchemaCheck(
            **base,
            status="unreachable",
            messages=["schema not checked — the connection's database could not be reached"],
        )
    return TemplateSchemaCheck(**base, status="ok")


async def check_template_schema(
    cfg: AppConfig, actor: Principal, templates_yaml: Optional[str]
) -> TemplateSchemaCheckResult:
    """On-demand check of every query template's referenced tables/columns
    against the *currently-live* connections' reflected schema — the existence
    check the offline dry-run deliberately skips (it never opens a DB session,
    like `explain`/cost-estimation). Best-effort: a connection that can't be
    reached yields an `unreachable` result, never an error, so a down database
    can never block staging otherwise-valid config. A field left unset inherits
    the active version's templates, matching the rest of the config plane."""
    start = time.monotonic()
    store = get_config_version_store()
    _, _, _, resolved_templates = _resolve_candidate(
        cfg,
        store,
        connections_yaml=None,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=templates_yaml,
    )
    if resolved_templates is None:
        return TemplateSchemaCheckResult(checked=False, note="No query templates are configured.")
    try:
        template_store = TemplateStore.from_dict(yaml.safe_load(resolved_templates) or {})
    except Exception:
        audit_config_change(
            action="check_template_schema",
            outcome="rejected",
            principal=actor.subject,
            principal_scopes=sorted(actor.scopes),
            auth_method=actor.auth_method,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return TemplateSchemaCheckResult(
            checked=False,
            note="The templates document is not structurally valid — run Validate (dry-run) first.",
        )

    results = [await _check_one_template_schema(t) for t in template_store.list()]
    audit_config_change(
        action="check_template_schema",
        outcome="success",
        principal=actor.subject,
        principal_scopes=sorted(actor.scopes),
        auth_method=actor.auth_method,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return TemplateSchemaCheckResult(checked=True, results=results)


def validate(
    cfg: AppConfig,
    principal: Optional[Principal] = None,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
    templates_yaml: Optional[str] = None,
) -> List[str]:
    start = time.monotonic()
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog, resolved_templates = (
        _resolve_candidate(
            cfg,
            store,
            connections_yaml=connections_yaml,
            policy_yaml=policy_yaml,
            catalog_yaml=catalog_yaml,
            templates_yaml=templates_yaml,
        )
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog, resolved_templates
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
    templates_yaml: Optional[str] = None,
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
    resolved_connections, resolved_policy, resolved_catalog, resolved_templates = (
        _resolve_candidate(
            cfg,
            store,
            connections_yaml=connections_yaml,
            policy_yaml=policy_yaml,
            catalog_yaml=catalog_yaml,
            templates_yaml=templates_yaml,
        )
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog, resolved_templates
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
        ConfigDocumentPreview(
            document="templates",
            change=change(templates_yaml, resolved_templates, active.templates_yaml),
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
    templates_yaml: Optional[str] = None,
    description: Optional[str],
) -> ConfigVersion:
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog, resolved_templates = (
        _resolve_candidate(
            cfg,
            store,
            connections_yaml=connections_yaml,
            policy_yaml=policy_yaml,
            catalog_yaml=catalog_yaml,
            templates_yaml=templates_yaml,
        )
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog, resolved_templates
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
        templates_yaml=resolved_templates,
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
        cfg,
        version.connections_yaml,
        version.policy_yaml,
        version.catalog_yaml,
        version.templates_yaml,
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
        # Query templates (item 48 phase 2) are now a governed document, so a
        # version carries its own templates snapshot and apply/rollback loads
        # exactly that. A version staged before phase 2 has no snapshot
        # (paths.templates is None); fall back to the deployment's static
        # template file rather than clobbering it to empty on an old rollback.
        template_file=paths.templates if paths.templates is not None else cfg.template_file,
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
