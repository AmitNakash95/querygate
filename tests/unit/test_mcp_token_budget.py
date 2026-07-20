"""Regression guardrail on the combined size of MCP_INSTRUCTIONS plus every
registered tool's JSON Schema — the fixed overhead sent to every MCP
session's initialize/tools-list exchange, regardless of which tools a
caller ever uses.

Nothing else in the codebase bounds this today (see TODO.md item 66), so a
verbose Field(description=...) or a new tool can silently regrow the
context cost of every single session. This test measures the same total a
2026-07-20 audit measured and fails loudly if it grows past a budget with
modest headroom, forcing any real increase to be a visible, deliberate
bump to _MAX_TOTAL_CHARS in the same PR rather than an unnoticed
regression.
"""

from __future__ import annotations

import json

from querygate.mcp.instructions import MCP_INSTRUCTIONS
from querygate.mcp.server import create_mcp_server

# Baseline measured 2026-07-20, post item 61 (merging execute_structured_query
# / explain_structured_query / execute_structured_queries into one
# run_structured_queries tool) and item 67 (restoring StructuredQuery
# Field(description=...) coverage items 62/64/65 assumed already existed —
# 10 of 11 top-level fields genuinely had none): 53,841 chars (~13,460
# tokens at 4 chars/token) across MCP_INSTRUCTIONS + all 12 registered tool
# schemas — down from the original pre-tranche 67,338 chars despite item 67
# adding real content, because item 61 collapsed StructuredQuery's $defs
# tree from 3 duplicated copies to 1. Budget adds ~15% headroom for
# near-term legitimate growth (e.g. a new dialect or tool). Bump this
# deliberately, in the same PR as the change that needs it — this is meant
# to make growth a visible review decision, not a silent one.
#
# Bumped 2026-07-20 (items 68-72: WHERE/HAVING guardrail caps, DISTINCT,
# table aliases/self-joins, NOT groups + column-to-column comparisons,
# whitelisted scalar functions/CASE) — measured 63,201 chars, a deliberate
# increase from real new StructuredQuery surface (from_alias, JoinSpec.alias,
# Predicate.value_col, ScalarFunctionSelectItem/CaseSelectItem/ColArg/
# LiteralArg and their Field descriptions, plus one short mcp/instructions.py
# self-join section), not drift. New budget again keeps ~5% headroom.
#
# Bumped 2026-07-20 (item 80: string_agg aggregate function) — measured
# 67,022 chars, from StringAggSelectItem's two new Field(description=...)
# entries joining the SelectItem union. New budget keeps modest headroom.
#
# Bumped 2026-07-20 (item 81: array_agg aggregate function) — measured
# 67,822 chars, from ArrayAggSelectItem's two new Field(description=...)
# entries joining the SelectItem union. New budget keeps modest headroom.
#
# Bumped 2026-07-20 (item 48: query templates) — the two new MCP tools
# list_query_templates/run_query_template (their descriptions plus the
# PublicQueryTemplate/parameter output schemas) add a deliberate new
# agent-facing surface on top of item 81's array_agg. New budget keeps modest
# headroom.
#
# Re-bumped 2026-07-20 (item 48 merge into main) — the 74,500 above was
# measured on the item-48 branch in isolation; merging it on top of main's
# meanwhile-grown tool surface combines both, measured 75,682 chars. Not
# drift — the additive sum of two independently-approved surfaces. New budget
# restores ~5% headroom.
_MAX_TOTAL_CHARS = 79_500


def _tool_schema_chars(tool: object) -> int:
    description = getattr(tool, "description", None) or ""
    parameters = json.dumps(getattr(tool, "parameters", None) or {})
    output_schema = json.dumps(getattr(tool, "output_schema", None) or {})
    return len(description) + len(parameters) + len(output_schema)


def test_total_mcp_context_overhead_stays_under_budget():
    server = create_mcp_server()
    tools = server._tool_manager._tools

    instructions_chars = len(MCP_INSTRUCTIONS.strip())
    tools_chars = sum(_tool_schema_chars(tool) for tool in tools.values())
    total_chars = instructions_chars + tools_chars

    assert total_chars <= _MAX_TOTAL_CHARS, (
        f"MCP instructions + tool schemas now total {total_chars} chars, "
        f"over the {_MAX_TOTAL_CHARS} budget (instructions={instructions_chars}, "
        f"tools={tools_chars}). If this growth is intentional, bump "
        "_MAX_TOTAL_CHARS in this file as part of the same change."
    )
