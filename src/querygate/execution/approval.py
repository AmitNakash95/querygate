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
  signature, an expired token, a fingerprint mismatch, or a connection/principal
  binding mismatch all return `False`, so the gate denies rather than admits on
  any ambiguity.
- The HMAC comparison is constant-time (`hmac.compare_digest`).
- The token never contains query values or any secret — only a fingerprint
  (a hash), the issuing approver's subject (for audit), an expiry, and (item
  151) the connection id and bound principal subject the token is valid for.

Connection/principal binding (TODO.md item 151, Decision Log in
`docs/PRODUCT_GUIDE.md`): `issue_approval_token`/`verify_approval_token` take
required `connection_id`/`principal_subject` keyword arguments (no default —
a call site must make a deliberate choice, even if that choice is `None`, so a
future call site can never silently *omit* binding by forgetting a keyword
argument the way an optional-with-default one can). When either is not `None`
at issue time it is carried as a new `"cx"`/`"sub_bind"` claim **alongside**
the existing `fp`/`sub`/`exp` claims — additive, not a replacement, so the
fingerprint computation and every already-issued token's shape stay stable.
When a token carries a `cx`/`sub_bind` claim, verification requires the caller
to supply a matching value or the token is rejected (fail-closed) — this is
what stops a token approved for query `Q` on connection `staging` from
verifying against the byte-identical `Q` on connection `prod`, and what stops
an approval token/MRTR pending state from being redeemed by a *different*
principal than the one whose subject it was bound to (not a session, and not
an agent-delegation `actor` — see `Principal` in `core/auth.py`; two delegated
sessions acting for the same human subject are not distinguished by this
check, which is a deliberate, narrower scope than session-binding). A token
issued with both `None` (the pre-item-151 shape, still exercised by tests that
only care about fingerprint semantics) carries no `cx`/`sub_bind` claim and is
verified exactly as before — the binding check only activates for claims the
token actually carries.

Token kind (`"k"` claim) and format version (`"v"` claim), also item 151: a
*pending* MRTR elicitation token (`mcp/elicitation.py`'s
`build_pending_input_required`) is only ever an integrity-protected marker
that a fingerprint/connection/principal was *asked about* — never itself a
grant — so it is minted with `kind="pending"` and can only verify against
`expected_kind="pending"`; every real grant (REST's `/query/approve`, the MCP
resolver's post-approval mint) uses the default `kind="grant"`/
`expected_kind="grant"`. Without this, a pending token decoded straight off
the wire and replayed as `X-QueryGate-Approval` (or in `approval_tokens`)
would satisfy every other check — its `fp`/`cx`/`sub_bind` are all genuine —
and execute without anyone ever having answered the elicitation prompt.
`"v"` is set to `1` unconditionally and checked at verify time so a token
minted by a pod running an older build (mid-rolling-deploy) that predates
these two claims is never treated as unbound-and-therefore-permissive by a
newer pod that does understand them.
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


# Token format version. Bumped whenever the claim shape changes in a way that
# affects verification semantics (item 151 added "cx"/"sub_bind"/"k"/"v"
# itself) — a token missing "v" entirely predates this claim and is rejected
# outright by `verify_approval_token`, rather than falling back to the older,
# less-restrictive verification behavior.
TOKEN_FORMAT_VERSION = 1

# Token kinds (the "k" claim). "grant" is a real, redeemable approval; "pending"
# is an MRTR elicitation's integrity-protected "this fingerprint/connection/
# principal was asked about" marker — never itself redeemable. Keeping these
# as a closed two-value set (not a bool) leaves room for a future kind without
# a payload-shape change.
TOKEN_KIND_GRANT = "grant"
TOKEN_KIND_PENDING = "pending"


def issue_approval_token(
    *,
    fingerprint: str,
    approver_subject: str,
    key: str,
    connection_id: Optional[str],
    principal_subject: Optional[str],
    kind: str = TOKEN_KIND_GRANT,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    """Mint an HMAC-signed approval token binding `fingerprint` + an expiry.

    Only ever called from the scope-gated approve endpoint (the caller must hold
    `query:approve`) or the MCP elicitation flow. `approver_subject` is recorded
    in the token for the audit trail, not used for enforcement. Raises
    `ValueError` if no key is configured — a deployment that enables the
    approval gate MUST set an HMAC key, otherwise no approval could ever be
    granted (fail-closed).

    `connection_id`/`principal_subject` (TODO.md item 151) are required
    keyword arguments — pass `None` explicitly to mint a token unbound on that
    axis (the pre-item-151 shape, only for tests that exercise fingerprint
    semantics in isolation); every production call site supplies a real value
    for both. Carried as `"cx"`/`"sub_bind"` in the payload when not `None` —
    additive alongside the existing `fp`/`sub`/`exp` claims, never folded into
    the fingerprint itself.

    `kind` (item 151) is `"grant"` by default; the MCP elicitation flow's
    pending-state mint is the only caller that passes `"pending"`. Always
    carried as `"k"`, and `"v"` (`TOKEN_FORMAT_VERSION`) is always carried too
    — both required at verify time, unconditionally, not just when set.
    """
    if not key:
        raise ValueError("APPROVAL_TOKEN_HMAC_KEY is not configured; cannot issue approval tokens")
    issued = time.time() if now is None else now
    payload: dict = {
        "v": TOKEN_FORMAT_VERSION,
        "fp": fingerprint,
        "sub": approver_subject,
        "k": kind,
        "exp": int(issued + ttl_seconds),
    }
    if connection_id is not None:
        payload["cx"] = connection_id
    if principal_subject is not None:
        payload["sub_bind"] = principal_subject
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _sign(payload_bytes, key.encode("utf-8"))
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{encoded}.{signature}"


def verify_approval_token(
    token: str,
    *,
    fingerprint: str,
    key: str,
    connection_id: Optional[str],
    principal_subject: Optional[str],
    expected_kind: str = TOKEN_KIND_GRANT,
    now: Optional[float] = None,
) -> bool:
    """Return True iff `token` is a valid, unexpired approval for exactly
    `fingerprint`. Fail-closed: any malformed/forged/expired/mismatched token,
    or a missing key, returns False — never raises to the caller.

    `connection_id`/`principal_subject` (item 151) are checked against the
    token's `"cx"`/`"sub_bind"` claims **only when the token carries them**: a
    token minted with a `cx`/`sub_bind` claim is rejected unless the caller
    supplies the matching value here (including when the caller omits it
    entirely), which is what stops a token approved for one connection/
    principal from verifying against a different one. A token minted without
    those claims verifies on fingerprint/expiry alone, as before item 151.

    `expected_kind`/`"v"` (item 151) are checked unconditionally, not just
    when present: a token whose `"k"` claim doesn't equal `expected_kind`
    (default `"grant"` — callers verifying a REST/MCP redemption never pass
    anything else; only the MCP elicitation resolver's pending-state check
    passes `"pending"`) or whose `"v"` claim is missing/mismatched is rejected
    outright. This is what stops an MRTR *pending* token — which carries a
    genuine `fp`/`cx`/`sub_bind` but was never actually approved by anyone —
    from being replayed directly as a real grant.
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
    if payload.get("v") != TOKEN_FORMAT_VERSION:
        return False
    if payload.get("fp") != fingerprint:
        return False
    if payload.get("k") != expected_kind:
        return False
    token_cx = payload.get("cx")
    if token_cx is not None and token_cx != connection_id:
        return False
    token_sub_bind = payload.get("sub_bind")
    if token_sub_bind is not None and token_sub_bind != principal_subject:
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    current = time.time() if now is None else now
    return current < exp
