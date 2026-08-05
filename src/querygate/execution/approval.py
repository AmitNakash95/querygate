"""In-query human-in-the-loop approval gate (TODO.md item 92, phase 1).

Some reads should not run unattended just because they pass policy: a query
whose pre-execution estimate is very large is the exfiltration leg of the
"lethal trifecta", and QueryGate is uniquely positioned to gate it on *what the
query would actually touch* (the planner's row/cost estimate) **before** it
runs. This module is that gate's decision logic, kept separate from the
execution pipeline so it can be reasoned about and tested in isolation.

Phase 1 (this module) triggers on the **cost/row estimate** — reusing the
estimate `execution/service.py` already computes — and grants approval via a
**stateless, HMAC-signed approval token**: no server-side approval store, no new
statefulness. An approver holding the `query:approve` scope issues a token bound
to a specific query's fingerprint and a short expiry; the original caller
re-submits the identical query with that token and the gate lets it through.
Because the token is HMAC-signed it cannot be forged, because it carries the
query fingerprint it cannot be replayed against a *different* query, and because
it carries an expiry it cannot be replayed indefinitely.

Phase 2 (not in this module yet): the catalog **sensitivity-label** trigger
(`sensitivity: pii`) and the interactive **MCP elicitation** channel. The token
format and gate below are designed to carry that second trigger without a
format change (the reasons list is already free-form).

Security notes:
- Verification is **fail-closed**: a missing key, a malformed token, a bad
  signature, an expired token, or a fingerprint mismatch all return `False`, so
  the gate denies rather than admits on any ambiguity.
- The HMAC comparison is constant-time (`hmac.compare_digest`).
- The token never contains query values or any secret — only a fingerprint
  (a hash), the issuing approver's subject (for audit), and an expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, List, Optional

from querygate.catalog.loader import get_catalog_store
from querygate.catalog.models import SensitivityClass
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.schema_validation import (
    declared_cte_names,
    effective_name_map,
    iter_column_refs,
    iter_query_scopes,
    parse_column_ref,
)

# Default lifetime of an issued approval token. Short by design: an approval is
# for "this query, now", not a standing grant.
DEFAULT_APPROVAL_TTL_SECONDS = 300


def approval_required_reasons(estimate: QueryCostEstimate, policy: Policy) -> List[str]:
    """Human-readable reasons this estimate trips the approval gate, or an empty
    list if it is within the (softer) approval thresholds. Mirror-shaped to
    `cost_estimation.cost_estimate_violations` so the two gates describe an
    over-threshold estimate the same way.

    Note the approval thresholds are independent of the hard
    `max_estimated_rows`/`max_estimated_cost` caps: approval is meant to sit
    *below* a hard reject (ask a human) — a deployment sets the approval
    threshold lower than the enforce threshold, or uses one without the other.
    """
    reasons: List[str] = []
    if (
        policy.approval_max_estimated_rows is not None
        and estimate.estimated_rows is not None
        and estimate.estimated_rows > policy.approval_max_estimated_rows
    ):
        reasons.append(
            f"estimated rows ({estimate.estimated_rows}) exceeds approval threshold "
            f"({policy.approval_max_estimated_rows})"
        )
    if (
        policy.approval_max_estimated_cost is not None
        and estimate.estimated_total_cost is not None
        and estimate.estimated_total_cost > policy.approval_max_estimated_cost
    ):
        reasons.append(
            f"estimated planner cost ({estimate.estimated_total_cost:.0f}) exceeds approval "
            f"threshold ({policy.approval_max_estimated_cost:.0f})"
        )
    return reasons


def sensitivity_approval_reasons(
    query: StructuredQuery, policy: Policy, connection_id: str
) -> List[str]:
    """Reasons the query touches a catalog-labelled sensitive column/table whose
    label is in `policy.approval_sensitivities` (item 92 phase 2). Enumerates
    every referenced column via the single canonical AST visitor (`iter_column_refs`,
    item 96), resolves each to its physical table, and consults the descriptive
    catalog's static sensitivity label — a read of metadata only, never a row
    value. Empty when the sensitivity trigger is unconfigured or nothing matches.

    A column's own label wins; otherwise its table's table-level label applies,
    so labelling a whole table sensitive covers columns without their own label.

    Walks EVERY scope (`iter_query_scopes`) — the outer query, every set-operation
    arm (item 104) and every nested `value_subquery` (item 97) — each against its
    own alias map, since an alias only means anything inside the scope that
    declares it. Reading the outer scope alone would have let a labelled column be
    reached from an arm or a subquery with the approval gate never firing; that
    hole was live for `value_subquery` from item 97 until item 104 closed it.
    """
    triggers = set(policy.approval_sensitivities)
    if not triggers:
        return []
    store = get_catalog_store()
    cte_names = declared_cte_names(query)
    hits: List[str] = []
    seen: set = set()
    for _depth, scope in iter_query_scopes(query):
        name_to_physical = effective_name_map(scope)
        for column_ref in iter_column_refs(scope):
            table, column = parse_column_ref(column_ref.ref)
            physical = name_to_physical.get(table.casefold(), table)
            # A cte name (item 105) is not a table, so it has no catalog entry to
            # carry a label. Skipped explicitly rather than left to `get_table`
            # returning None, because a catalog entry that happened to share the
            # block's name would otherwise report a hit naming a table this query
            # never read. No trigger is lost: the block's body is its own scope in
            # this same walk, and that is where its real columns are labelled.
            if physical.casefold() in cte_names:
                continue
            entry = store.get_table(connection_id, physical)
            if entry is None:
                continue
            col_entry = entry.column(column)
            label = (
                col_entry.sensitivity
                if col_entry is not None and col_entry.sensitivity != SensitivityClass.NONE
                else entry.sensitivity
            )
            if label in triggers:
                key = f"{physical}.{column}"
                if key not in seen:
                    seen.add(key)
                    hits.append(f"references {label}-labelled column {key}")
    return hits


def query_fingerprint(query: StructuredQuery) -> str:
    """A stable SHA-256 fingerprint of the *entire* validated query AST.

    An approval token is bound to this value, so approving one query never
    authorizes a different one — even a one-character change to a predicate
    value or a column changes the fingerprint and invalidates the token. Uses
    canonical (sorted-key, no-whitespace) JSON of the Pydantic model so the
    fingerprint is deterministic across processes and Python runs.
    """
    return _model_fingerprint(query)


def write_fingerprint(statement: Any) -> str:
    """Stable SHA-256 fingerprint of a validated write statement (item 93 phase
    2), so an approval token binds to exactly this write — the write-side sibling
    of `query_fingerprint`, same canonical-JSON scheme. Typed loosely to avoid
    coupling the approval module to `write_ast`."""
    return _model_fingerprint(statement)


def _model_fingerprint(model: Any) -> str:
    canonical = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sign(payload: bytes, key: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue_approval_token(
    *,
    fingerprint: str,
    approver_subject: str,
    key: str,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    """Mint an HMAC-signed approval token binding `fingerprint` + an expiry.

    Only ever called from the scope-gated approve endpoint (the caller must hold
    `query:approve`). `approver_subject` is recorded in the token for the audit
    trail, not used for enforcement. Raises `ValueError` if no key is
    configured — a deployment that enables the approval gate MUST set an HMAC
    key, otherwise no approval could ever be granted (fail-closed).
    """
    if not key:
        raise ValueError("APPROVAL_TOKEN_HMAC_KEY is not configured; cannot issue approval tokens")
    issued = time.time() if now is None else now
    payload = {
        "fp": fingerprint,
        "sub": approver_subject,
        "exp": int(issued + ttl_seconds),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _sign(payload_bytes, key.encode("utf-8"))
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{encoded}.{signature}"


def verify_approval_token(
    token: str,
    *,
    fingerprint: str,
    key: str,
    now: Optional[float] = None,
) -> bool:
    """Return True iff `token` is a valid, unexpired approval for exactly
    `fingerprint`. Fail-closed: any malformed/forged/expired/mismatched token,
    or a missing key, returns False — never raises to the caller.
    """
    if not key or not token:
        return False
    try:
        encoded, signature = token.rsplit(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload_bytes = base64.urlsafe_b64decode(encoded + padding)
        expected = _sign(payload_bytes, key.encode("utf-8"))
        if not hmac.compare_digest(expected, signature):
            return False
        payload = json.loads(payload_bytes)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("fp") != fingerprint:
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    current = time.time() if now is None else now
    return current < exp
