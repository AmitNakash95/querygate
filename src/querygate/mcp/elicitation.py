"""Shared MCP in-session approval via elicitation (TODO.md item 92 / 93 / 128).

Both the read tool (`run_structured_queries`) and the write tool
(`run_structured_writes`) let a client's human approve a gated operation
in-session, instead of the out-of-band REST token flow. Ported to the
`2026-07-28` Multi Round-Trip Requests (MRTR) model: a server may no longer
send a JSON-RPC request mid-call (`Context.elicit` — the old blocking
round-trip — no longer exists on v2's `Context`), so a batch that trips the
gate returns `InputRequiredResult` instead, and the client retries the exact
same tool call carrying `input_responses`/`request_state`.

**Two-phase design, both phases reusing `execution/approval.py`'s existing
signing primitives verbatim — no new crypto:**

1. `build_pending_input_required` — called when one or more batch items raise
   `ApprovalRequiredError` on the first pass. For each, mints a *pending*
   token via `issue_approval_token` (the exact same function/signature the
   REST out-of-band flow and the old resolver used) — it is not itself a
   grant, only an integrity-protected binding of "this key asked about this
   fingerprint", bundled into `request_state` (the spec requires `request_state`
   be attacker-controlled-and-integrity-protected; reusing the existing
   HMAC token IS that protection, not a new format).
2. `resolve_approval_tokens_from_retry` — called on the retry. Verifies each
   pending token's signature (fail-closed on any expired/forged/malformed
   entry), **re-derives the fingerprint from the resubmitted AST and compares**
   (item 128's mutation-verified enforcement point: without this check, a
   caller could obtain approval for query A and replay the state against a
   *different* query B in the same key slot), then — only if the client's
   `input_responses` for that key says `approve: true` — mints a REAL grant
   (a fresh `issue_approval_token`, `approver_subject` recording the
   elicitation identity for audit) the caller retries `execute_many`/
   `write execute_many` with via their existing `approval_tokens` map.

Off by default (`AppConfig.mcp_elicitation_approval_enabled`): an elicitation
response has no authenticated approver identity, so treating it as an
approval is an explicit operator decision that the client's human is a
trusted approver. QueryGate has no way to verify a human was actually asked —
`ctx.input_responses` is asserted by the client host, and a compromised or
malicious client can complete this channel with no human involvement at all.
Unlike the REST out-of-band flow (separation of duties by scope: the
approving principal must hold a distinct scope from the querying one), this
channel's only real guarantee is the fingerprint-binding above; it is off by
default specifically because the "only a human can approve" property is the
client's to honor, not something QueryGate can enforce server-side.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.logging import get_logger
from querygate.execution.approval import (
    TOKEN_KIND_PENDING,
    issue_approval_token,
    verify_approval_token,
)

# Deliberately short: the "pending" token only needs to survive the round
# trip to the client and back, not stand as a durable grant. Independent of
# DEFAULT_APPROVAL_TTL_SECONDS, which governs the REAL grant minted in
# resolve_approval_tokens_from_retry.
_PENDING_STATE_TTL_SECONDS = 300

# Recorded in the pending token's `sub` field for audit-trail readability only
# (naming it as a pending marker, not a human approver, if it ever surfaces in
# a log). NOT what stops a pending token from being redeemed as a real grant —
# that is the `"k"`/`kind="pending"` claim (`execution/approval.py`, item 151):
# `verify_approval_token`'s `expected_kind` check rejects a pending token
# presented anywhere a real grant (`expected_kind="grant"`, the default) is
# required, independent of what this constant's string happens to say.
_PENDING_SUBJECT = "mcp:pending-elicitation"


class ApprovalElicitation(BaseModel):
    """The one-field form an MCP client renders for an in-session approval
    step-up. Primitive-only per the elicitation spec."""

    approve: bool = Field(
        default=False,
        description=(
            "Set true to approve this sensitive/expensive/large operation for a "
            "single execution. Leaving it false (or declining/cancelling) rejects it."
        ),
    )


_APPROVAL_SCHEMA = render_elicitation_schema(ApprovalElicitation)


def build_pending_input_required(
    *,
    items: List[Tuple[str, str, List[str]]],
    config: AppConfig,
    caller: Principal,
    connection_id: str,
) -> Optional[InputRequiredResult]:
    """Build the `InputRequiredResult` for a batch's pending approvals.

    `items` is `(key, fingerprint, reasons)` per gated item — `key` is the
    caller-assigned slot (e.g. `"q0"`) `input_responses`/the retry's
    per-item result correlate back to. Returns `None` (stay fail-closed on
    the REST token flow) unless the operator opted in *and* a signing key is
    set, or there is nothing pending.

    `caller`/`connection_id` (TODO.md item 151) bind the pending state's
    `sub_bind`/`cx` claims to the *calling principal's own subject* — not a
    session identifier, and not an agent-delegation `actor` — and the
    connection of the tool call that tripped approval. This is what stops a
    `request_state` handed from this session to a *different principal's*
    agent session from being honored for that principal's identical retry at
    `resolve_approval_tokens_from_retry`, even though it decodes and its
    fingerprint still matches; two sessions delegating for the *same* human
    subject are not distinguished by this check. `approver_subject` stays the
    constant `_PENDING_SUBJECT` (unchanged, audit-only, never enforced). The
    real reason this can never be redeemed as a grant is `kind=TOKEN_KIND_PENDING`
    below — see `execution/approval.py`'s module docstring.
    """
    if not config.mcp_elicitation_approval_enabled or not config.approval_token_hmac_key:
        return None
    if not items:
        return None

    input_requests: Dict[str, ElicitRequest] = {}
    pending_tokens: Dict[str, str] = {}
    for key, fingerprint, reasons in items:
        reason_text = "; ".join(reasons) if reasons else "policy requires approval"
        message = (
            "This operation needs human approval before it runs "
            f"({reason_text}). Approve this one-time execution?"
        )
        input_requests[key] = ElicitRequest(
            params=ElicitRequestFormParams(message=message, requested_schema=_APPROVAL_SCHEMA)
        )
        pending_tokens[key] = issue_approval_token(
            fingerprint=fingerprint,
            approver_subject=_PENDING_SUBJECT,
            key=config.approval_token_hmac_key,
            connection_id=connection_id,
            principal_subject=caller.subject,
            kind=TOKEN_KIND_PENDING,
            ttl_seconds=_PENDING_STATE_TTL_SECONDS,
        )
    request_state = json.dumps(pending_tokens, sort_keys=True, separators=(",", ":"))
    return InputRequiredResult(input_requests=input_requests, request_state=request_state)


def _is_approved(response: object) -> bool:
    if not isinstance(response, ElicitResult):
        return False
    if response.action != "accept":
        return False
    content = response.content
    return bool(content and content.get("approve") is True)


def resolve_approval_tokens_from_retry(
    *,
    ctx: Context,
    fingerprints_by_key: Dict[str, str],
    caller: Principal,
    config: AppConfig,
    connection_id: str,
) -> Dict[str, str]:
    """On an MRTR retry, return `{fingerprint: real_approval_token}` for every
    key whose pending `request_state` entry validates against the freshly
    re-derived fingerprint of the resubmitted item AND whose
    `input_responses` says `approve: true`. Fails closed (empty dict, or a
    key silently omitted) on any malformed/expired/forged/mismatched entry —
    never raises.

    `fingerprints_by_key` must be computed by the caller from the *just-
    resubmitted* AST (the exact same computation the first pass used to
    build `items` for `build_pending_input_required`) — comparing against it
    is what stops a caller from obtaining approval for one query and
    replaying the granted state against a different one in the same slot.

    `caller`/`connection_id` (TODO.md item 151) must match the `sub_bind`/`cx`
    claims `build_pending_input_required` bound the pending state to — this is
    what stops a `request_state` handed to a *different principal's* agent
    session from being honored for that principal's identical tool call, even
    though it decodes and its fingerprint still matches (`sub_bind` binds to
    `Principal.subject`, not a session id — two sessions delegating for the
    same human subject are not distinguished). The resulting REAL grant is
    bound to the same `caller`/`connection_id` too, since it is redeemed by
    this same retry inside the same tool call. The pending token being
    verified must also carry `kind=TOKEN_KIND_PENDING` — a real grant
    presented here (which should never happen, but is checked anyway) is
    rejected the same as a forged one, and the freshly-minted real grant below
    always uses the default `kind="grant"`.
    """
    if not config.mcp_elicitation_approval_enabled or not config.approval_token_hmac_key:
        return {}
    if ctx.request_state is None or not ctx.input_responses:
        return {}

    log = get_logger()
    try:
        pending_tokens = json.loads(ctx.request_state)
    except (ValueError, TypeError):
        log.warning("approval.elicitation.malformed_request_state")
        return {}
    if not isinstance(pending_tokens, dict):
        log.warning("approval.elicitation.malformed_request_state")
        return {}

    resolved: Dict[str, str] = {}
    declined_fingerprints: set = set()
    for key, fingerprint in fingerprints_by_key.items():
        pending = pending_tokens.get(key)
        if not isinstance(pending, str):
            continue
        # The mutation-verified enforcement point (TODO.md item 128, trap 1):
        # the pending token must validate against THIS item's freshly
        # re-derived fingerprint, not just decode successfully. A caller that
        # swaps in a different query at the same key fails here. Item 151
        # adds connection_id/caller.subject to that same check: a pending
        # state bound to a different connection or principal fails here too.
        # expected_kind=TOKEN_KIND_PENDING additionally rejects anything that
        # isn't genuinely a pending marker.
        if not verify_approval_token(
            pending,
            fingerprint=fingerprint,
            key=config.approval_token_hmac_key,
            connection_id=connection_id,
            principal_subject=caller.subject,
            expected_kind=TOKEN_KIND_PENDING,
        ):
            log.warning("approval.elicitation.pending_state_invalid", key=key)
            continue
        approved = _is_approved(ctx.input_responses.get(key))
        log.info(
            "approval.elicitation.decision",
            key=key,
            fingerprint=fingerprint,
            approved=approved,
        )
        if not approved:
            # `resolved` is keyed by fingerprint, not by slot (execute_many
            # looks a statement's token up by its own fingerprint) — a
            # duplicate statement submitted twice in one batch shares a
            # fingerprint across two keys. A decline at either slot must veto
            # that fingerprint everywhere, or an approval minted for the
            # other, identical slot would let the declined one execute too.
            declined_fingerprints.add(fingerprint)
            continue
        resolved[fingerprint] = issue_approval_token(
            fingerprint=fingerprint,
            approver_subject=f"mcp-elicitation:{caller.subject}",
            key=config.approval_token_hmac_key,
            connection_id=connection_id,
            principal_subject=caller.subject,
        )
    for fingerprint in declined_fingerprints:
        resolved.pop(fingerprint, None)
    return resolved
