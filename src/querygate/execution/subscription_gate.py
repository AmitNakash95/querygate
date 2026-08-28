"""The one place `execution/` reaches into `subscription/`.

A single shared helper rather than three imports, because the forbidden-edge
guard (`tests/unit/test_subscription_boundaries.py`) allows exactly
``execution/`` → ``subscription.gate`` and nothing else, and because the three
funnels must not drift apart in what they check.

**Three funnels, not two** — decided 2026-08-23:

1. **Reads** — ``StructuredQueryService._validate_and_compile``, whose own
   comment already names it as "the four ways an ad-hoc AST reaches a database".
2. **Writes** — the three write *service* entry points. ``WriteExecutionService``
   and ``WritePreviewService`` are separate classes and ``_execute_many_atomically``
   opens its own ``session_scope``, so a gate only in ``StructuredQueryService``
   would leave every governed INSERT/UPDATE/DELETE running.
3. **Caller-initiated schema discovery** — ``list_tables``, ``describe_table``
   and ``search_catalog`` reach the live database through reflection and pass
   through *neither* other funnel: ``_validate_and_compile``'s comment is precise
   that it covers the ways an *AST* reaches a database, and these carry no AST.
   They are product, not diagnostics, so an expired deployment enumerates
   nothing **for a caller**.

   The **background catalog refresh** (``CatalogRefreshMonitor``) is
   deliberately *not* gated here, and this docstring used to claim it was. It
   runs on a timer with a ``system:`` principal, so a 402 has no caller to
   reach; gating it would mean a lapsed deployment's refresh loop either raises
   on every tick or silently stops, and neither is a state an operator can read
   off a metric. The residual — a lapsed deployment keeps reflecting its own
   schema into its own catalog file, disclosing nothing to anyone — is recorded
   in ``docs/THREAT_MODEL.md`` and tracked as TODO.md item 223 rather than left
   as a sentence that is not true.

The gate is deliberately **not** in ``validation/write_policy_validation``. That
is a pure function of (AST, Policy) with no I/O and no process state; gating
there would make every shape-validation unit test depend on the subscription
singleton, and would hand a 402 to any non-executing caller — a linter, the admin
candidate simulator, a dry-run — for asking whether a *shape* is legal.
"""

from __future__ import annotations

from ..subscription.gate import require_active_subscription

#: One label per funnel, so the observe-mode metric says which enforcement point
#: would have refused. Low cardinality and fixed.
FUNNEL_READ = "read"
FUNNEL_WRITE = "write"
FUNNEL_SCHEMA = "schema_discovery"


def check_read_funnel() -> None:
    """The read funnel proper: `_validate_and_compile`, which every AST path runs
    through — `execute`, `explain`, `verdict`, and their batch forms. This is the
    call that owns the observe-mode metric."""
    require_active_subscription(funnel=FUNNEL_READ)


def check_read_admission() -> None:
    """The same check, run earlier in `execute` and deliberately not counted.

    Its only job is ordering: refuse before the quota reservation and the
    concurrency slot, so an expired deployment does not spend a caller's quota
    or hold a slot per refused request. `check_read_funnel` counts.
    """
    require_active_subscription(funnel=FUNNEL_READ, count=False)


def check_write_funnel() -> None:
    require_active_subscription(funnel=FUNNEL_WRITE)


def check_schema_discovery_funnel() -> None:
    require_active_subscription(funnel=FUNNEL_SCHEMA)
