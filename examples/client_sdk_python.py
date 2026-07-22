"""Author QueryGate structured queries with the typed Python builder (item 51).

`examples/rest_calls.md` shows the raw `StructuredQuery` JSON a caller writes
by hand. This is the same contract with typed method calls, editor
autocomplete, and build-time validation instead of hand-written dicts — the
builder constructs the very same models the server validates, so it can never
produce a shape the server would accept but the builder wouldn't (or vice
versa), and it adds no trust: whatever it emits is still policy- and
schema-checked server-side before any row is touched.

The builder ships inside the installed `querygate` package:

  from querygate.client import Query, agg, col, desc

No server or network is needed to *build* a query — everything below through
`.to_dict()` runs fully offline and is what `tests/unit/test_client_builder.py`
verifies. Sending one needs a running QueryGate (see README.md:
`poetry run uvicorn querygate.api.app:app`, default demo connection `demo`);
the optional `--send` path at the bottom is the only part that needs the
server, and is left as a thin `httpx` call you can point at your deployment.

Run:
  python examples/client_sdk_python.py            # print the built JSON bodies
  python examples/client_sdk_python.py --send     # also POST them to a local server
"""

from __future__ import annotations

import json
import sys

from querygate.client import Query, agg, and_, case, col, desc, fn_select, lit, when

# NOTE: the demo policy (examples/policy.example.yaml) masks orders.total_amount
# so it may only appear as a bare SELECT projection, and denies customers.email.
# These examples deliberately stay within that policy so `--send` is a clean
# happy path — but the point of QueryGate is that the *server* enforces this
# regardless of what the builder emits: point any of these at a masked column
# in a filter/aggregate and you get a policy 422, exactly as intended.


def orders_per_customer() -> Query:
    """Aggregate + join + filter + order — the everyday reporting query."""
    return (
        Query.from_("orders")
        .join("customers", on=("orders.customer_id", "customers.id"))
        .select("customers.name", agg.count("*", as_="order_count"))
        .where(col("customers.country") == "GB")
        .where(col("orders.status") == "completed")
        .group_by("customers.name")
        .order_by("order_count", desc=True)
        .limit(5)
        .intent("GB customers ranked by completed-order count")
    )


def order_item_value_buckets() -> Query:
    """A CASE expression, a scalar function, and a boolean WHERE group — the
    kind of shape that is fiddly to get right as raw JSON."""
    return (
        Query.from_("order_items")
        .select(
            "order_items.id",
            fn_select("upper", col("order_items.product_name"), as_="product_upper"),
            case(
                when(col("order_items.quantity") >= 100, lit("bulk")),
                when(col("order_items.quantity") >= 10, lit("wholesale")),
                else_=lit("retail"),
                as_="tier",
            ),
        )
        .where(
            and_(
                col("order_items.unit_price") > 0,
                col("order_items.quantity").is_not_null(),
            )
        )
        .order_by("order_items.quantity", desc=True)
        .limit(20)
    )


def top_line_items_per_order() -> Query:
    """An aggregate with per-partition top-N ranking."""
    return (
        Query.from_("order_items")
        .select("order_items.order_id", agg.avg("order_items.unit_price", as_="avg_price"))
        .group_by("order_items.order_id")
        .top_n(3, order_by=[desc("avg_price")])
    )


EXAMPLES = {
    "orders_per_customer": orders_per_customer,
    "order_item_value_buckets": order_item_value_buckets,
    "top_line_items_per_order": top_line_items_per_order,
}


def _send(connection: str, body: dict) -> None:
    import os

    import httpx

    # Send the caller's own bearer token if the server requires auth. With the
    # shipped .env (API_KEYS='["local-admin-key"]'):
    #   QUERYGATE_API_KEY=local-admin-key python examples/client_sdk_python.py --send
    headers = {}
    token = os.environ.get("QUERYGATE_API_KEY")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"http://localhost:8000/api/v1/{connection}/query"
    resp = httpx.post(url, json=body, headers=headers, timeout=30.0)
    print(f"  -> HTTP {resp.status_code}")
    try:
        print("  ", json.dumps(resp.json(), indent=2, default=str)[:800])
    except Exception:  # noqa: BLE001 - example diagnostics only
        print("  ", resp.text[:800])


def main() -> None:
    send = "--send" in sys.argv[1:]
    for name, factory in EXAMPLES.items():
        body = factory().to_dict()
        print(f"\n# {name}")
        print(json.dumps(body, indent=2, default=str))
        if send:
            _send("demo", body)


if __name__ == "__main__":
    main()
