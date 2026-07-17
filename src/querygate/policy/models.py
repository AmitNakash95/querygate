"""Per-connection policy — the enforcement surface between an agent and real SQL.

A `Policy` is resolved per connection (see policy/loader.py) and checked by
validation/policy_validation.py before a query is ever compiled or executed.
"""

from __future__ import annotations

from typing import Optional

import pydantic as pyd


class MandatoryRowFilter(pyd.BaseModel):
    """A static equality filter always AND-ed into every query that touches
    `table` — a generic, policy-declared replacement for hardcoding a
    "scope every query to this tenant" rule into the query AST itself.
    """

    table: str
    column: str
    value: object

    model_config = pyd.ConfigDict(extra="forbid")


class Policy(pyd.BaseModel):
    enabled: bool = True

    # Table/column allow-deny. An empty allow-list means "no restriction";
    # deny always wins over allow. Column lists are keyed by table name, with
    # "*" applying to every table.
    allowed_tables: list[str] = pyd.Field(default_factory=list)
    denied_tables: list[str] = pyd.Field(default_factory=list)
    allowed_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)
    denied_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)

    # Query complexity caps.
    max_joins: int = pyd.Field(default=5)
    max_select_columns: int = pyd.Field(default=30)
    max_where_depth: int = pyd.Field(default=5)
    max_group_by: int = pyd.Field(default=10)
    max_limit: int = pyd.Field(default=100)
    max_limit_aggregate: int = pyd.Field(default=1000)
    default_limit: int = pyd.Field(default=50)
    max_top_n: int = pyd.Field(default=50)
    max_partition_by: int = pyd.Field(default=5)
    max_batch_size: int = pyd.Field(default=10)

    # Execution guardrails.
    timeout_seconds: int = pyd.Field(default=30)
    max_concurrency: int = pyd.Field(default=8)
    concurrency_wait_seconds: float = pyd.Field(default=10)

    # Cross-connection joins: two connections may be joined in one query only
    # when they resolve to the same join_group (defaults to the connection's
    # own id, i.e. no cross-connection joins unless explicitly configured).
    join_group: Optional[str] = None

    mandatory_row_filters: list[MandatoryRowFilter] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    def table_allowed(self, table_name: str) -> bool:
        name = table_name.lower()
        if any(t.lower() == name for t in self.denied_tables):
            return False
        if self.allowed_tables:
            return any(t.lower() == name for t in self.allowed_tables)
        return True

    def column_allowed(self, table_name: str, column_name: str) -> bool:
        col = column_name.lower()
        denied = self.denied_columns.get(table_name, []) + self.denied_columns.get("*", [])
        if any(c.lower() == col for c in denied):
            return False
        allowed_specific = self.allowed_columns.get(table_name)
        allowed_wildcard = self.allowed_columns.get("*")
        allowed = allowed_specific if allowed_specific is not None else allowed_wildcard
        if allowed is not None:
            return any(c.lower() == col for c in allowed)
        return True
