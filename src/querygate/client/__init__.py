"""QueryGate typed client-side query builder (TODO.md item 51, phase 1).

A pure-authoring convenience for constructing QueryGate's ``StructuredQuery``
AST with typed method calls and editor autocomplete instead of hand-written
raw JSON. It builds the *same* ``query_ast`` Pydantic models the server
validates, so it adds no validation of its own and cannot weaken any
server-side guardrail — see ``querygate.client.builder`` for the details.

Ships inside the ``querygate`` package for now; a standalone, dependency-light
distribution (and a TypeScript sibling) are tracked as item 51 phase 2.

Typical use::

    from querygate.client import Query, agg, col, desc

    body = (
        Query.from_("orders")
        .join("customers", on=("orders.customer_id", "customers.id"))
        .select("customers.name", agg.sum("orders.total_amount", as_="total_spend"))
        .where(col("customers.country") == "GB")
        .group_by("customers.name")
        .order_by("total_spend", desc=True)
        .limit(10)
        .to_dict()
    )
    # POST body -> /api/v1/<connection>/query
"""

from querygate.client.builder import (
    Column,
    Expr,
    FnColumn,
    Literal,
    Query,
    agg,
    and_,
    array_agg,
    asc,
    case,
    case_expr,
    cast,
    col,
    col_fn,
    date_add,
    date_bucket,
    desc,
    expr,
    expr_fn,
    expr_select,
    extract,
    fn,
    fn_select,
    frame,
    lit,
    not_,
    now,
    or_,
    percentile_cont,
    string_agg,
    when,
    window,
)

__all__ = [
    "Query",
    "col",
    "col_fn",
    "lit",
    "fn",
    "agg",
    "date_bucket",
    "string_agg",
    "array_agg",
    "percentile_cont",
    "fn_select",
    "case",
    "when",
    "and_",
    "or_",
    "not_",
    "asc",
    "desc",
    "expr",
    "expr_fn",
    "expr_select",
    "case_expr",
    "cast",
    "extract",
    "now",
    "date_add",
    "window",
    "frame",
    "Column",
    "FnColumn",
    "Literal",
    "Expr",
]
