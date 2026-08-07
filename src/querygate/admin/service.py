"""Orchestrates config-governance validate/stage/apply for the admin API.

Layered on top of the same primitives item 5 (config_reload) and item 17
(cli.validate_config) already built — applying a version is exactly
`reload_config()` pointed at that version's files, and validating a
candidate is exactly `validate_config()` pointed at a scratch directory.
Neither of those gets a parallel implementation here.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pydantic as pyd
import sqlalchemy as sa
import yaml

from querygate.admin.access_diff import compute_access_diff
from querygate.admin.blast_radius import compute_blast_radius_report
from querygate.admin.models import (
    CandidateColumnDecision,
    CandidatePolicySimulation,
    CandidatePolicySimulationRequest,
    CandidateSimulationReason,
    ConfigApprovalDecision,
    ConfigChangeSetBundle,
    ConfigChangeSetImportCheck,
    ConfigDocumentPreview,
    ConfigPreview,
    ConfigSemanticDiffRequest,
    ConfigVersion,
    ConfigVersionStatus,
    DraftSummary,
    EffectiveGuardrails,
    MandatoryFilterReadiness,
    PolicyBlastRadiusReport,
    SemanticAccessDiff,
    TemplateSchemaCheck,
    TemplateSchemaCheckResult,
)
from querygate.admin.draft_store import get_draft_store
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
    ServiceDisabledError,
)
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE
from querygate.policy.models import Policy
from querygate.secrets.resolvers import build_secret_resolver_registry
from querygate.templates.binding import dummy_bound_query
from querygate.templates.loader import TemplateStore
from querygate.templates.models import QueryTemplate
from querygate.validation.policy_validation import (
    referenced_tables_tree_wide,
    validate_policy,
)
from querygate.validation.schema_validation import (
    resolve_query_table_connections,
    resolve_scope_connections,
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
    # TODO.md item 156: the same per-scope table-to-connection map
    # `execution/service.py`'s `_validate_and_compile` computes before calling
    # `validate_policy` in production, computed here with the CANDIDATE
    # resolver so the simulator's verdict can't drift from what a real
    # cross-connection query against this candidate config would actually
    # enforce. Best-effort: a connection-resolution failure here is swallowed
    # and reported properly by the dedicated `resolve_query_table_connections`
    # check below (`query_connection_denied`), which already owns that error
    # shape — this local variable only needs to exist so `validate_policy`
    # gets a real map on the success path.
    scope_connections: Dict[int, Dict[str, str]] = {}
    if request.query is not None:
        # Tree-wide (item 121): a mandatory filter whose table appears only in a
        # set-op arm or a subquery must still show up in the readiness report,
        # or an operator is told `allow` for a request execution will refuse.
        requested_tables.update(referenced_tables_tree_wide(request.query))
        query_allowed = True
        try:
            scope_connections = resolve_scope_connections(
                request.query,
                request.connection,
                principal=target,
                connection_resolver=candidate_resolver,
            )
        except (NotFoundError, QueryValidationError, ConfigValidationError):
            # A bad cross-connection reference — including, since TODO.md
            # item 163, a join to a candidate secondary connection whose
            # dialect isn't yet connectable (Snowflake/BigQuery) — the
            # dedicated check below reports this properly; validate_policy
            # still runs (against no map, i.e. primary-only) so an unrelated
            # policy violation is not masked by a connection problem it
            # didn't cause.
            scope_connections = {}
        try:
            # TODO.md item 145: capture the purpose-narrowed effective Policy
            # (unchanged if the query declares no purpose) so the readiness
            # report below reflects a purpose delta's own mandatory_row_filters
            # — mirroring execution/service.py's identical reassignment. Without
            # it, a purpose-declaring query's readiness was simulated against
            # the un-narrowed policy, silently omitting a filter real
            # execution would apply (found by `security-invariant-reviewer`/
            # `architecture-boundary-reviewer`, 2026-08-05).
            #
            # TODO.md item 156: `principal`/`scope_connections`/
            # `connection_resolver` let this consult a cross-connection join's
            # table's OWN connection's Policy too — the same union this
            # module's own `resolve_query_table_connections` check just below
            # already gates the join's mere existence on — so the simulator's
            # `allow`/`deny` verdict agrees with what `execution/service.py`
            # would actually enforce (found by `architecture-boundary-
            # reviewer`/`security-invariant-reviewer`, 2026-08-06: this call
            # site was the one place still stuck on primary-Policy-only after
            # item 156 fixed every other enforcement path).
            policy = validate_policy(
                request.query,
                policy,
                connection_id=request.connection,
                principal=target,
                scope_connections=scope_connections,
                connection_resolver=candidate_resolver,
            )
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
            except (NotFoundError, QueryValidationError, ConfigValidationError):
                query_allowed = False
                reasons.append(
                    CandidateSimulationReason(
                        code="query_connection_denied",
                        message=(
                            "The structured query references a connection that is not visible "
                            "to the target principal, is outside the candidate join group, or "
                            "is a dialect QueryGate cannot yet open a live connection for."
                        ),
                    )
                )

    # TODO.md item 156: gather every connection's candidate Policy the query
    # actually touches (the primary plus every distinct connection any scope's
    # own map resolves to) BEFORE deciding which tables/filters are visible —
    # both the visibility check just below and the filter-readiness walk
    # after it need the same list, and computing it once keeps them from
    # silently disagreeing on which connections are in play.
    candidate_filter_policies: list[Policy] = [policy]
    seen_connections = {request.connection}
    for table_connection in scope_connections.values():
        for cx in table_connection.values():
            if cx in seen_connections:
                continue
            seen_connections.add(cx)
            try:
                _profile, other_policy = candidate_resolver(cx, target)
            except NotFoundError:
                continue
            candidate_filter_policies.append(other_policy)

    # Do not enumerate mandatory-filter identifiers for a table the target
    # policy itself hides. Requested names may still receive a table-denied
    # decision, but no additional hidden policy metadata rides with it.
    #
    # TODO.md item 156: checked against EVERY candidate policy in play, not
    # just the primary connection's — a table denied only by a cross-
    # connection join's joined connection's own Policy must be hidden from
    # this report too, or a principal already told `deny` for that table
    # would still learn a mandatory-filter column name for it. Coarser than
    # attributing each table to its own precise connection (a table reached
    # through more than one connection in a multi-way join is hidden if ANY
    # candidate denies it, not only the one that actually governs that
    # occurrence) — the safe direction, matching this module's existing
    # fail-closed posture for the identical single-connection case.
    requested_table_keys = {
        table.casefold()
        for table in requested_tables
        if all(candidate.table_allowed(table) for candidate in candidate_filter_policies)
    }

    filter_readiness: list[MandatoryFilterReadiness] = []
    seen_filters: set = set()
    for candidate_policy in candidate_filter_policies:
        for row_filter in candidate_policy.mandatory_row_filters:
            if row_filter.table.casefold() not in requested_table_keys:
                continue
            if id(row_filter) in seen_filters:
                continue
            seen_filters.add(id(row_filter))
            ready = True
            if row_filter.from_claim is not None:
                try:
                    row_filter.resolve(target)
                except PolicyViolationError:
                    ready = False
                    reasons.append(
                        CandidateSimulationReason(
                            code="mandatory_claim_missing",
                            message=(
                                "A mandatory row-filter claim is missing for the target "
                                "principal."
                            ),
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

    result = compute_blast_radius_report(
        active_context, candidate_context, principal_offset=request.principal_offset
    )
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


def safe_pydantic_error_lines(exc: pyd.ValidationError) -> List[str]:
    """Turn a `pydantic.ValidationError` into safe, human-readable lines built
    strictly from each error's `loc`/`msg` -- never `input`/`input_value`.

    Companion to `_humanize_validation_errors` above, for callers that hold
    the live `ValidationError` object rather than a pre-stringified error
    list: `error["input_value"]` can embed the *entire* validated object for
    certain pydantic error kinds (e.g. a `missing`-type error), which is
    dangerous whenever the validated object may already contain a resolved
    secret -- e.g. `ConnectionProfile.model_validate()` on an
    already-`${...}`-interpolated `connections.yaml` entry (item 165). The
    actual guarantee is that this function only ever reads `loc`/`msg` from
    each error dict -- `include_input=False` is a belt-and-braces request to
    pydantic to not even materialize `input`/`input_value` in the first
    place, on top of (not instead of) that."""
    lines: List[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ()))
        msg = error.get("msg", "")
        lines.append(f"{loc}: {msg}" if loc else msg)
    return lines


def safe_yaml_error_detail(exc: yaml.YAMLError) -> str:
    """Turn a `yaml.YAMLError` into a safe, human-readable summary that never
    calls `str()` on the exception itself, or on its `.problem_mark`/
    `.context_mark`.

    PyYAML's `Mark.__str__` embeds `get_snippet()` -- the literal offending
    SOURCE LINE -- which for a `connections.yaml` parse failure can be a
    line containing an already-interpolated, credential-bearing
    `connection_string` (item 165: confirmed directly, an unterminated
    quote on a `connection_string:` line reproduces the password in
    `str(yaml.YAMLError)`). Only the plain string fields (`.context`/
    `.problem`/`.note`) and the mark's integer `.line`/`.column` are used --
    never the mark's own `__str__`, and never `.name` (which can be a
    caller-controlled stream/file label)."""
    parts: List[str] = []
    context = getattr(exc, "context", None)
    if context:
        parts.append(str(context))
    problem = getattr(exc, "problem", None)
    if problem:
        parts.append(str(problem))
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None:
        parts.append(f"at line {mark.line + 1}, column {mark.column + 1}")
    note = getattr(exc, "note", None)
    if note:
        parts.append(str(note))
    if not parts:
        parts.append("Invalid YAML syntax")
    return "; ".join(parts)


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
    except ConfigValidationError as exc:
        # TODO.md item 163: a template whose bound query cross-connection
        # joins to a secondary connection QueryGate cannot yet open a live
        # connection for (Snowflake/BigQuery) — reported as a per-template
        # issue, not raised past this function, so one such template can
        # never abort the whole batch's results or its audit event.
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


# --- Config change-set bundles (item 47) -----------------------------------

_DOCUMENT_ORDER: Tuple[str, ...] = ("connections", "policy", "catalog", "templates")


def _version_fingerprint(
    connections_yaml: str,
    policy_yaml: str,
    catalog_yaml: Optional[str],
    templates_yaml: Optional[str],
) -> str:
    """A stable content hash over a version's four documents.

    Each document is length-prefixed and a missing (None) document is framed
    distinctly from an empty-string one, so no combination of contents can
    collide with another. Used only to detect that an import's base version
    has drifted — never as a security boundary.
    """
    hasher = hashlib.sha256()
    for value in (connections_yaml, policy_yaml, catalog_yaml, templates_yaml):
        if value is None:
            hasher.update(b"\x00none\x00")
        else:
            encoded = value.encode("utf-8")
            hasher.update(f"\x01{len(encoded)}\x02".encode("ascii"))
            hasher.update(encoded)
    return hasher.hexdigest()


def export_change_set(
    cfg: AppConfig,
    principal: Principal,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
    templates_yaml: Optional[str] = None,
    description: Optional[str],
) -> ConfigChangeSetBundle:
    """Package the caller's submitted document deltas into a portable bundle.

    Only the documents actually submitted (non-None) are included — the bundle
    is a change set, not a full config snapshot — and it is stamped with the
    current active version's id and content fingerprint so a later import can
    tell whether the target has drifted. The response echoes only content the
    caller supplied; inherited (unset) documents are never resolved into it,
    so this never discloses the active connections/policy content.
    """
    store = get_config_version_store()
    active = _bootstrap(cfg, store)

    submitted: Dict[str, Optional[str]] = {
        "connections": connections_yaml,
        "policy": policy_yaml,
        "catalog": catalog_yaml,
        "templates": templates_yaml,
    }
    documents = {name: value for name, value in submitted.items() if value is not None}

    bundle = ConfigChangeSetBundle(
        base_version_id=active.id,
        base_fingerprint=_version_fingerprint(
            active.connections_yaml,
            active.policy_yaml,
            active.catalog_yaml,
            active.templates_yaml,
        ),
        created_at=datetime.now(timezone.utc),
        description=description,
        documents=documents,  # type: ignore[arg-type]
    )
    audit_config_change(
        action="export",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=active.id,
        description=description,
    )
    return bundle


def import_change_set(
    cfg: AppConfig,
    principal: Principal,
    bundle: ConfigChangeSetBundle,
) -> ConfigChangeSetImportCheck:
    """Validate an uploaded change-set bundle and report drift, before staging.

    This never persists anything and never stages on its own — it resolves the
    bundle's deltas over the current active version, validates the result with
    the exact loaders the live pipeline uses, and reports a content-free change
    signal plus whether the active base has drifted from the bundle's
    fingerprint. Staging still goes through the existing `/versions` endpoint,
    so there is one governed mutation path, not a shadow store.
    """
    start = time.monotonic()
    store = get_config_version_store()
    active = _bootstrap(cfg, store)

    errors: List[str] = []
    warnings: List[str] = []

    total_bytes = sum(len(value.encode("utf-8")) for value in bundle.documents.values())
    if total_bytes > cfg.config_bundle_max_bytes:
        errors.append(
            f"change set is too large ({total_bytes} bytes; the limit is "
            f"{cfg.config_bundle_max_bytes} bytes)"
        )

    documents_map = {name: value for name, value in bundle.documents.items()}
    # Resolve the deltas over the active base (unset documents inherit).
    resolved_connections = documents_map.get("connections", active.connections_yaml)
    resolved_policy = documents_map.get("policy", active.policy_yaml)
    resolved_catalog = documents_map.get("catalog", active.catalog_yaml)
    resolved_templates = documents_map.get("templates", active.templates_yaml)

    if not errors:
        errors = validate_candidate_content(
            cfg, resolved_connections, resolved_policy, resolved_catalog, resolved_templates
        )

    # Stale-base detection: compare the bundle's recorded base fingerprint with
    # the target's current active fingerprint. If the caller omitted a
    # fingerprint we can't judge drift and say so, rather than implying a match.
    current_fingerprint = _version_fingerprint(
        active.connections_yaml, active.policy_yaml, active.catalog_yaml, active.templates_yaml
    )
    stale_base = False
    base_conflict_documents: List[str] = []
    if bundle.base_fingerprint is None:
        warnings.append(
            "bundle carries no base fingerprint; drift against the active version "
            "could not be checked"
        )
    elif bundle.base_fingerprint != current_fingerprint:
        stale_base = True
        active_docs = {
            "connections": active.connections_yaml,
            "policy": active.policy_yaml,
            "catalog": active.catalog_yaml,
            "templates": active.templates_yaml,
        }
        # Re-derive which base documents moved by fingerprinting the bundle's
        # base id, if it still exists, against today's active content.
        base_docs = active_docs
        try:
            base_version = (
                store.get_version(bundle.base_version_id) if bundle.base_version_id else None
            )
        except NotFoundError:
            base_version = None
        if base_version is not None:
            base_docs = {
                "connections": base_version.connections_yaml,
                "policy": base_version.policy_yaml,
                "catalog": base_version.catalog_yaml,
                "templates": base_version.templates_yaml,
            }
            for name in _DOCUMENT_ORDER:
                if base_docs[name] != active_docs[name]:
                    base_conflict_documents.append(name)
        warnings.append(
            "the active version has changed since this change set was created; "
            "review the differences before staging"
        )

    documents = [
        ConfigDocumentPreview(
            document=name,  # type: ignore[arg-type]
            change="submitted" if name in documents_map else "inherited",
        )
        for name in _DOCUMENT_ORDER
    ]

    contains_connections = bundle.contains_connections
    if contains_connections:
        warnings.append(
            "this change set includes a connections document, which may contain a "
            "literal credential — it is never stored in the browser"
        )

    audit_config_change(
        action="import",
        outcome="rejected" if errors else "success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=bundle.base_version_id,
        description=bundle.description,
        error_category="validation" if errors else None,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return ConfigChangeSetImportCheck(
        valid=not errors,
        errors=errors,
        documents=documents,
        stale_base=stale_base,
        base_conflict_documents=base_conflict_documents,  # type: ignore[arg-type]
        contains_connections=contains_connections,
        ready_to_stage=not errors and bool(documents_map),
        warnings=warnings,
    )


def save_draft(cfg: AppConfig, principal: Principal, bundle: ConfigChangeSetBundle) -> DraftSummary:
    """Persist `bundle` server-side, encrypted at rest, owned by the caller
    (item 47 phase 2). Raises `ServiceDisabledError` when the store is
    disabled (no encryption key configured), or `ConfigValidationError` when
    the caller is already at `draft_store_max_drafts_per_principal`."""
    start = time.monotonic()
    store = get_draft_store(cfg)
    if store is None:
        raise ServiceDisabledError(
            "the server-side draft store is not configured on this deployment"
        )
    try:
        summary = store.save(
            principal_id=principal.subject,
            bundle=bundle,
            description=bundle.description,
            retention_seconds=cfg.draft_store_retention_seconds,
            max_drafts=cfg.draft_store_max_drafts_per_principal,
        )
    except ConfigValidationError as exc:
        audit_config_change(
            action="save_draft",
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            description=bundle.description,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise
    audit_config_change(
        action="save_draft",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        description=bundle.description,
        draft_id=summary.id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return summary


def list_my_drafts(cfg: AppConfig, principal: Principal) -> List[DraftSummary]:
    """The caller's own saved drafts, metadata only — never audited as its
    own action, matching the unaudited `GET /versions` list precedent."""
    store = get_draft_store(cfg)
    if store is None:
        raise ServiceDisabledError(
            "the server-side draft store is not configured on this deployment"
        )
    return store.list_for_principal(principal.subject)


def load_draft(cfg: AppConfig, principal: Principal, draft_id: str) -> ConfigChangeSetBundle:
    """Decrypt and return one of the caller's own saved drafts. Raises
    `NotFoundError` uniformly for "doesn't exist", "expired", and "exists but
    belongs to someone else" — never an existence/ownership oracle."""
    start = time.monotonic()
    store = get_draft_store(cfg)
    if store is None:
        raise ServiceDisabledError(
            "the server-side draft store is not configured on this deployment"
        )
    try:
        bundle = store.load(draft_id, principal_id=principal.subject)
    except NotFoundError:
        audit_config_change(
            action="load_draft",
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            draft_id=draft_id,
            error_category="not_found",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise
    audit_config_change(
        action="load_draft",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        draft_id=draft_id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return bundle


def delete_draft(cfg: AppConfig, principal: Principal, draft_id: str) -> None:
    """Delete one of the caller's own saved drafts. Same uniform
    `NotFoundError` posture as `load_draft`."""
    start = time.monotonic()
    store = get_draft_store(cfg)
    if store is None:
        raise ServiceDisabledError(
            "the server-side draft store is not configured on this deployment"
        )
    try:
        store.delete(draft_id, principal_id=principal.subject)
    except NotFoundError:
        audit_config_change(
            action="delete_draft",
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            draft_id=draft_id,
            error_category="not_found",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise
    audit_config_change(
        action="delete_draft",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        draft_id=draft_id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )


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

    # Four-eyes gate (item 42): a staged version's FIRST activation needs the
    # required number of distinct approvals. Rollback (reactivating a version
    # that was already active) is deliberately exempt — it was approved when
    # first applied, and gating DR/rollback on re-approval would be unsafe.
    if action == "apply" and cfg.require_config_approvals > 0:
        approvals = _count_valid_approvals(version)
        if approvals < cfg.require_config_approvals:
            audit_config_change(
                action=action,
                outcome="rejected",
                principal=principal.subject,
                principal_scopes=sorted(principal.scopes),
                auth_method=principal.auth_method,
                version_id=version_id,
                error_category="insufficient_approvals",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise PolicyViolationError(
                f"Version {version_id} has {approvals} of the {cfg.require_config_approvals} "
                "required approvals (from distinct reviewers other than the author) and "
                "cannot be applied yet."
            )

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


def _count_valid_approvals(version: ConfigVersion) -> int:
    """Distinct reviewers who currently *approve* this version's exact content.

    Bound to the version's content fingerprint so an approval can never count
    for different content (defense-in-depth — staged content is immutable, so
    the fingerprint always matches today). A reviewer whose latest decision is a
    rejection does not count (the store already keeps only one record per
    reviewer). The author is structurally excluded because the store refuses to
    record an author's own review at all.
    """
    fingerprint = _version_fingerprint(
        version.connections_yaml,
        version.policy_yaml,
        version.catalog_yaml,
        version.templates_yaml,
    )
    return len(
        {
            a.approver
            for a in version.approvals
            if a.decision == ConfigApprovalDecision.APPROVE and a.content_fingerprint == fingerprint
        }
    )


def approve(
    cfg: AppConfig,
    principal: Principal,
    version_id: str,
    *,
    decision: ConfigApprovalDecision,
    note: Optional[str] = None,
) -> ConfigVersion:
    """Record a four-eyes review decision on a staged version (item 42).

    The caller must hold `admin:config:approve` (enforced at the route). The
    author-≠-approver and staged-only invariants are enforced in the store and
    surface as `PolicyViolationError`; every decision is audited (content-free).
    """
    store = get_config_version_store()
    version = store.get_version(version_id)  # raises NotFoundError -> 404 at the route
    start = time.monotonic()
    fingerprint = _version_fingerprint(
        version.connections_yaml,
        version.policy_yaml,
        version.catalog_yaml,
        version.templates_yaml,
    )
    action = "approve" if decision == ConfigApprovalDecision.APPROVE else "reject"
    try:
        updated = store.add_approval(
            version_id,
            approver=principal.subject,
            decision=decision,
            content_fingerprint=fingerprint,
            note=note,
        )
    except PolicyViolationError:
        audit_config_change(
            action=action,
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            version_id=version_id,
            error_category="separation_of_duties",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise
    audit_config_change(
        action=action,
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=version_id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return updated


def get_current(cfg: AppConfig) -> ConfigVersion:
    store = get_config_version_store()
    return _bootstrap(cfg, store)


def list_versions(cfg: AppConfig) -> List[ConfigVersion]:
    store = get_config_version_store()
    _bootstrap(cfg, store)
    return store.list_versions()


def get_version(version_id: str) -> ConfigVersion:
    return get_config_version_store().get_version(version_id)
