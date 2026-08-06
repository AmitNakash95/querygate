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
#
# Bumped 2026-07-23 (item 93 phase 2b: governed-writes MCP tool) — the new
# run_structured_writes tool inlines the whole write AST (Insert/Update/Delete
# plus the reused WHERE tree) and the WritePreview/WriteDiff response schema, a
# deliberate ~23K new agent-facing surface for the flagship write feature
# (measured 102,968 chars). New budget keeps ~5% headroom.
#
# Bumped 2026-07-25 (item 100: the bounded scalar Expression substrate) — the
# closed recursive Expression union (column/literal/arithmetic/function/cast/
# CASE) joins the SelectItem union, both sides of every Predicate, and every
# aggregate's argument, so it appears throughout the inlined StructuredQuery
# schema. Measured 117,100 chars; a ~14K deliberate increase for the flagship
# engine pillar. Before bumping, the new models' *maintainer* rationale was
# moved out of their docstrings into `#` comments (a Pydantic docstring becomes
# the agent-facing schema description and costs tokens on every session, a `#`
# comment costs nothing) — that trim alone recovered ~5K. New budget keeps ~5%
# headroom. Do the same trim before the next bump: prose that only a maintainer
# needs does not belong in a model docstring.
#
# Bumped 2026-07-25 (item 101: general window functions) — 123,000 -> 132,000,
# measured 126,700. Kept as one line so this log stays append-only; the full
# rationale (and the prediction the next entry redeems) is in
# docs/TODO_ARCHIVE.md's item-101 entry.
#
# DROPPED 2026-07-26 (item 114: the write tool stops advertising read-only
# predicate fields) — measured 104,042 chars, **down 24,509 (-19%)** from the
# 128,551 measured at item 115 (which had itself added 1,851 over item 101's
# 126,700 — a wider EffectiveGuardrails in describe_my_querygate_access's output
# schema — without
# needing a bump, so item 101's figure understates this drop).
# `run_structured_writes` fell from 38,964 to 14,455 (-63%): a
# write's `where` used to reuse the READ `Predicate`, which dragged the entire
# `Expression` union, every `SelectItem` (windows included) and the whole read
# `StructuredQuery` definition into a tool that REJECTS all of them. The write
# AST now has its own narrowed predicate types, so the schema advertises exactly
# what a write accepts. This is the drop item 101's archive entry predicted, and
# it is why that raise was accepted. Note precisely what improved: this ceiling
# (110,000) is back below item 100's 123,000 but is still ~2,000 ABOVE the 108,000
# in force before item 100 — it is the measured *total* that is now lower.
#
# NOT bumped 2026-07-26 (item 102: extract/now/date_add) — measured 108,329,
# which fits the existing 110,000. Recorded rather than left silent because it
# changes the advice below: headroom is now ~1.5%, not ~5%, so the NEXT engine
# item will have to raise this.
#
# Breakdown, MEASURED against each parent commit rather than attributed:
# ~3,400 for the three new `Expression` members, ~700 for the instructions
# section teaching the relative-date idiom, and **47** for the
# `"maximum": 2147483647, "minimum": -2147483648` fragment Pydantic emits from
# `DateAddExpr.amount`'s int32 bound — one occurrence, in run_structured_queries'
# params. An earlier version of this note credited that last item ~160 chars to
# the non-temporal-operand *rejection message*. That is false in a way anyone can
# check: `"date/time column"` appears 0 times across every tool description,
# parameter schema and output schema, because a runtime QueryValidationError
# string cannot reach an MCP schema at all. Left on the record because this note's
# whole job is steering the next budget decision, and the wrong version sends a
# maintainer to shorten error messages instead of auditing field constraints.
# The three nodes' maintainer rationale went into `#` comments, not model
# docstrings, per the note below — their agent-facing docstrings are 2-3 lines each.
#
# Item 103 (join `condition` + `full`/`cross` join types) cost a MEASURED 832
# chars — the enum's two new values, the `condition` field, and three tightened
# descriptions; `condition` is a `$ref` to the WhereNode already in `$defs`, so
# the recursive tree it unlocks is free. That is small, but it lands on an
# already-eroded budget: the total was 104,042 at item 114 and is 109,161 now,
# so headroom is **0.8%, not the ~5% this note assumes**, and the next engine
# item (104, set operations — a NEW top-level shape, not a `$ref` to an existing
# one) will breach it. That is a maintainer decision, deliberately not taken
# here: raise the ceiling, or do the trim pass the note below describes. The
# numbers above are recorded so it can be made on data.
#
# Bumped 2026-07-27 (item 104: set operations) — 110,000 -> 116,000. Every figure
# below was measured against base commit e735735 in a worktree, NOT derived:
#
#                                  base e735735   working tree     delta
#   instructions                          9,478         10,256      +778
#   tool schemas                         99,774        101,259    +1,485
#   TOTAL                               109,252        111,515    +2,263
#     run_structured_queries             39,692         41,090    +1,398
#     describe_my_querygate_access       10,599         10,686        +87
#
# This is the breach item 103's note predicted, and the schema half arrived far
# smaller than that note expected: **1,485 chars**, not the "new top-level shape"
# cost it warned of. That is a design outcome, not luck — `set_op` hangs off
# `StructuredQuery` with the carrying query as arm 1, so `SetOpSpec.arms` is a
# `$ref` to the StructuredQuery already in `$defs` rather than a second top-level
# query type. The 1,398 decomposes as a 1,145-char `SetOpSpec` `$def` plus a
# 226-char `set_op` property on `StructuredQuery`. The +87 is `max_set_op_arms`
# joining `GUARDRAIL_FIELDS` — worth naming because the advice below points at
# that schema as the next lever, and it grows on its own with every new cap.
#
# An earlier version of this entry said "1,576" and split the total as
# "110,737 AST/schema + 778 instructions". Both were wrong in precisely the way
# this comment already warns about once: 1,576 was 1,145 + 431, where the 431 was
# the `SetOpSpec` class docstring **already counted inside the 1,145**; and
# 110,737 was just 111,515 - 778, which folds 9,478 chars of PRE-EXISTING
# instructions into "the AST/schema half". Left on the record rather than quietly
# corrected, because a derived number presented as a measured one is the exact
# defect this comment exists to prevent.
#
# The trim the note below prescribes WAS attempted first, and found nothing to
# take: the largest remaining definitions in `run_structured_queries`' params
# ($defs StructuredQuery 3,995, Predicate 3,362, JoinSpec 2,521; the longest
# single descriptions are PercentileContSelectItem's 630 and CaseWhen's 561) are
# all agent-facing dialect/shape guidance, not maintainer rationale — item 100's
# trim already moved that into `#` comments, and item 104's new rationale went
# there from the start. So the ceiling is raised rather than the content shrunk,
# and the raise restores the ~4% headroom this note assumes instead of the 0.8%
# item 103 left. If the next item breaches this too, the honest lever left is
# the `describe_my_querygate_access` output schema (10,686 chars, and it grows
# automatically with every new `Policy` cap via GUARDRAIL_FIELDS) — not another
# pass over the query AST.
#
# Bumped 2026-07-27 (item 125: a window function as an `Expression` operand) —
# 116,000 -> 123,000. Measured against base commit 7721368 in a worktree, not
# derived:
#
#                                  base 7721368   working tree     delta
#   instructions                         10,263         10,263         0
#   tool schemas                        104,508        106,876    +2,368
#   TOTAL                               114,771        117,139    +2,368
#     run_structured_queries             44,159         46,527    +2,368
#
# The whole delta is in one tool's params, and instructions are untouched — item
# 125 added no agent-facing prose, only AST surface. It decomposes as a 1,766-char
# `WindowExpr` `$def` plus ~600 for the 18 `$ref` entries it adds wherever the
# `Expression` union appears.
#
# The prescribed trim ran FIRST and was worth 534 chars: `WindowCall` (the shared
# base split out so the projection and operand spellings cannot drift) was given no
# docstring at all, and `WindowExpr`'s was cut to the five lines a caller actually
# needs, with the maintainer rationale moved to `#` comments above each class. The
# figures above are post-trim; pre-trim the total was 117,673.
#
# Checked for the duplicated-definition case this note warns about, and it is NOT
# that: no `WindowCall` `$def` is emitted (0 references in the schema — Pydantic
# emits definitions for the concrete models only), and `WindowExpr` is a real
# second node with different legality from `WindowSelectItem`, not a copy. Worth
# recording one NEW lever it creates, though: every field description on
# `WindowCall` is now paid TWICE, once per subclass, so those five descriptions
# (~400 chars) cost ~800. They were left alone because they are agent-facing arity
# guidance, which this note's own rule says not to cut — but they are the first
# place to look if a future window change needs to claw chars back.
#
# Process note for the next maintainer: this breach was caught by CI, NOT by the
# local `pytest -m unit` run, because this file carries no `unit` marker and is
# deselected by that tier. That is TODO.md item 124 (~60% of tests/unit/ is invisible
# to the pre-commit gate), and this is a concrete instance of the cost.
#
# The budget keeps ~5% headroom. Before raising it again: check whether the
# growth is real new capability or another duplicated definition, and move
# maintainer rationale out of model docstrings into `#` comments first (a
# Pydantic docstring becomes the agent-facing schema description and costs
# tokens on every session; a `#` comment costs nothing).
#
# 2026-08-06 (TODO.md item 128): +412 chars from BatchQueryItemToolResult/
# WriteBatchItemResult gaining approval_fingerprint/approval_reasons —
# real new capability (the MCP MRTR approval port needs these visible in
# the output schema so a client can tell "this item needs approval" apart
# from any other error without parsing free text), not duplication.
#
# 2026-08-06 (TODO.md item 19 phase 2): +648 chars in `list_connections` and
# `describe_my_querygate_access` — `DatabaseDialect` gained a `snowflake`
# member (each tool's output schema embeds the enum once), real new
# capability (a caller can now see/reason about a Snowflake connection at
# all), not duplication.
_MAX_TOTAL_CHARS = 125_000


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
