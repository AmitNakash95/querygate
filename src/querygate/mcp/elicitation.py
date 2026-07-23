"""Shared MCP in-session approval via elicitation (TODO.md item 92 / 93).

Both the read tool (`run_structured_queries`) and the write tool
(`run_structured_writes`) let a client's human approve a gated operation
in-session via `Context.elicit`, instead of the out-of-band REST token flow. The
resolver here is generic over the `ApprovalRequiredError` — it mints a token
bound to whatever fingerprint the error carries (a read query fingerprint or a
write fingerprint) — so one implementation serves both surfaces.

Off by default (`AppConfig.mcp_elicitation_approval_enabled`): an elicitation
response has no authenticated approver identity, so treating it as an approval
is an explicit operator decision that the client's human is a trusted approver.
The querying/writing agent can never satisfy its own gate here — only a human
answering the elicitation can — and the minted token is fingerprint-bound, so it
can't be reused for anything else.
"""

from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.core.logging import get_logger
from querygate.execution.approval import issue_approval_token
from querygate.execution.service import ApprovalResolver


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


def build_elicitation_resolver(
    ctx: Context, caller: Principal, config: AppConfig
) -> Optional[ApprovalResolver]:
    """Build an `ApprovalResolver` that asks the client's human to approve a
    gated operation via `Context.elicit`, minting a one-time fingerprint-bound
    token on approval. Returns `None` (stay fail-closed on the REST token flow)
    unless the operator opted in *and* a signing key is set."""
    if not config.mcp_elicitation_approval_enabled or not config.approval_token_hmac_key:
        return None

    async def _resolve(operation, exc: ApprovalRequiredError) -> Optional[str]:
        reasons = "; ".join(exc.reasons) if exc.reasons else "policy requires approval"
        message = (
            "This operation needs human approval before it runs "
            f"({reasons}). Approve this one-time execution?"
        )
        log = get_logger()
        try:
            result = await ctx.elicit(message=message, schema=ApprovalElicitation)
        except Exception:
            # Client can't elicit (no interactive channel) — degrade to the
            # fail-closed rejection rather than failing the whole batch item.
            log.info("approval.elicitation.unavailable", fingerprint=exc.fingerprint)
            return None
        approved = result.action == "accept" and getattr(result.data, "approve", False)
        log.info(
            "approval.elicitation.decision",
            fingerprint=exc.fingerprint,
            action=result.action,
            approved=approved,
        )
        if not approved:
            return None
        return issue_approval_token(
            fingerprint=exc.fingerprint,
            approver_subject=f"mcp-elicitation:{caller.subject}",
            key=config.approval_token_hmac_key,
        )

    return _resolve
