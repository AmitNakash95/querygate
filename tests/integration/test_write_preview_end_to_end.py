"""Governed-writes dry-run preview against real SQLite (TODO.md item 93, Phase 1).

The critical property: a preview reports the affected-row count accurately and
**changes nothing** — the data is byte-identical before and after. Also proves
deny-by-default (writes off => clean policy rejection) and redaction-safety (the
returned SQL is parameterized, never literal values).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _enable_writes():
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["insert", "update", "delete"],
                    max_affected_rows=100000,
                )
            ),
            overrides={},
        )
    )


async def _all_orders(client) -> list:
    resp = await client.post(
        "/api/v1/demo/query",
        json={"from": "orders", "select": ["orders.id", "orders.status"], "limit": 100000},
    )
    return sorted(resp.json()["rows"], key=lambda r: r["id"])


@pytest.mark.asyncio
async def test_update_preview_reports_count_and_changes_nothing(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        # How many orders are currently 'pending' (the count the preview should report).
        pend = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["orders.id"],
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
                "limit": 100000,
            },
        )
        expected_affected = len(pend.json()["rows"])

        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "shipped"},
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation"] == "update"
        assert body["affected_rows"] == expected_affected
        assert body["executed"] is False
        assert "shipped" not in body["sql"]  # parameterized, no literal
        assert "pending" not in body["sql"]

        after = await _all_orders(client)
    # Nothing changed — the preview did not commit (or even execute the UPDATE).
    assert before == after
    assert expected_affected > 0  # the demo seed has pending orders — real matching happened


@pytest.mark.asyncio
async def test_delete_preview_changes_nothing(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["executed"] is False
        after = await _all_orders(client)
    assert before == after  # nothing deleted


async def _status_of(client, order_id: int) -> str:
    resp = await client.post(
        "/api/v1/demo/query",
        json={
            "from": "orders",
            "select": ["orders.id", "orders.status"],
            "where": {"col": "orders.id", "op": "eq", "value": order_id},
        },
    )
    return resp.json()["rows"][0]["status"]


@pytest.mark.asyncio
async def test_update_diff_shows_old_to_new_and_changes_nothing(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        target = before[0]["id"]
        old_status = await _status_of(client, target)

        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "diff-preview-new"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        assert resp.status_code == 200, resp.text
        diff = resp.json()["diff"]
        assert diff is not None
        assert len(diff["rows"]) == 1
        row = diff["rows"][0]
        # The diff shows the real old value and the proposed new value...
        assert row["before"]["status"] == old_status
        assert row["after"]["status"] == "diff-preview-new"
        assert row["before"]["id"] == target == row["after"]["id"]

        after = await _all_orders(client)
    # ...but the DML ran only inside a rolled-back transaction — nothing changed.
    assert before == after
    assert old_status != "diff-preview-new"


@pytest.mark.asyncio
async def test_delete_diff_lists_rows_that_would_be_removed(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 200, resp.text
        diff = resp.json()["diff"]
        assert diff["rows"] and all(
            r["before"] is not None and r["after"] is None for r in diff["rows"]
        )
        after = await _all_orders(client)
    assert before == after  # nothing deleted


@pytest.mark.asyncio
async def test_diff_is_bounded_by_max_diff_rows(sqlite_app):
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["delete"],
                    max_affected_rows=100000,
                    max_diff_rows=1,
                )
            ),
            overrides={},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        # Match every order (id > 0) so the affected set exceeds the diff cap of 1.
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.id", "op": "gt", "value": 0},
            },
        )
        diff = resp.json()["diff"]
    assert len(diff["rows"]) == 1  # capped
    assert diff["truncated"] is True
    assert diff["row_limit"] == 1


@pytest.mark.asyncio
async def test_diff_masks_masked_columns(sqlite_app):
    # A read-masked column must be redacted in the diff, not leaked as a value.
    set_policy_store(
        PolicyStore(
            default=Policy(
                column_masks={"orders": [{"column": "status", "kind": "hash"}]},
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["update"],
                    max_affected_rows=100000,
                ),
            ),
            overrides={},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "update",
                "table": "orders",
                "set": {"customer_id": 1},
                "where": {"col": "orders.id", "op": "eq", "value": 1},
            },
        )
        assert resp.status_code == 200, resp.text
        row = resp.json()["diff"]["rows"][0]
    assert row["before"]["status"] == "***MASKED***"
    assert row["after"]["status"] == "***MASKED***"


def _enable_writes_with_cap(max_affected_rows: int):
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["insert", "update", "delete"],
                    max_affected_rows=max_affected_rows,
                )
            ),
            overrides={},
        )
    )


def _record_sql(request) -> list:
    """Capture every statement the preview actually sends to the database.

    The `sqlite_app` fixture monkeypatches `get_engine` to return its real
    engine, so we can hook SQLAlchemy's cursor-execute event on it. Observing
    the SQL is the only way to prove item 108: the preview rolls back either
    way, so the *data* is unchanged whether or not the DML ran — the defect is
    the work performed, not the end state."""
    import querygate.execution.service as svc_module

    engine = svc_module.get_engine("demo")
    executed: list = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    request.addfinalizer(
        lambda: event.remove(engine.sync_engine, "before_cursor_execute", _on_execute)
    )
    return executed


def _updates(executed: list) -> list:
    return [s for s in executed if s.lstrip().upper().startswith("UPDATE")]


@pytest.mark.asyncio
async def test_over_cap_update_diff_never_runs_the_dml(sqlite_app, request):
    """TODO.md item 108: `include_diff=true` against a broad WHERE must not run a
    real row-locking UPDATE over every matching row just to preview a write that
    is rejected outright as over-cap."""
    _enable_writes_with_cap(2)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        assert len(before) > 2  # the predicate below really is over-cap

        executed = _record_sql(request)
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "over-cap-preview"},
                "where": {"col": "orders.id", "op": "gt", "value": 0},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        after = await _all_orders(client)

    assert body["affected_rows"] == len(before)
    assert body["within_affected_cap"] is False
    # The headline assertion: no UPDATE reached the database.
    assert _updates(executed) == [], f"over-cap preview ran DML: {_updates(executed)}"
    # The caller still gets a useful bounded diff — the fix skips the DML, not
    # the feature (computed by applying the SET in Python).
    assert body["diff"] is not None and body["diff"]["rows"]
    row = body["diff"]["rows"][0]
    assert row["after"]["status"] == "over-cap-preview"
    assert row["before"]["status"] != "over-cap-preview"
    assert before == after  # and nothing changed


@pytest.mark.asyncio
async def test_within_cap_update_diff_still_runs_the_dml(sqlite_app, request):
    """Positive control for item 108 — the guard must be the affected-row cap,
    not a blanket disabling of the DML-backed diff. Within cap the preview still
    executes the real UPDATE in the rolled-back transaction, which is what makes
    the diff reflect true committed shape."""
    _enable_writes_with_cap(100000)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        target = before[0]["id"]

        executed = _record_sql(request)
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "in-cap-preview"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["within_affected_cap"] is True
        after = await _all_orders(client)

    assert len(_updates(executed)) == 1
    assert before == after  # still rolled back


@pytest.mark.asyncio
async def test_over_cap_delete_diff_lists_rows_without_running_dml(sqlite_app, request):
    """A DELETE preview never ran the DML (it only SELECTs the doomed rows), so
    an over-cap DELETE keeps its useful bounded diff."""
    _enable_writes_with_cap(2)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        executed = _record_sql(request)
        resp = await client.post(
            "/api/v1/demo/write/preview?include_diff=true",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.id", "op": "gt", "value": 0},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        after = await _all_orders(client)

    assert body["within_affected_cap"] is False
    assert body["diff"]["rows"] and all(r["after"] is None for r in body["diff"]["rows"])
    assert [s for s in executed if s.lstrip().upper().startswith("DELETE")] == []
    assert before == after


@pytest.mark.asyncio
async def test_writes_denied_by_default_is_a_clean_rejection(sqlite_app):
    # Default policy (from conftest) has writes off — the preview must refuse.
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.id", "op": "eq", "value": 1},
            },
        )
    assert resp.status_code == 422  # PolicyViolationError -> 422
    assert "not enabled" in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# The write CONTRACT at the transport (TODO.md item 114). The narrowing is what
# an agent actually sees, so the refusal has to be asserted where the agent hits
# it — as a clean 4xx that leaks no internals — not only against the model.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read_only_field",
    [
        {"expr": {"op": "*", "left": {"col": "orders.id"}, "right": {"literal": 2}}},
        {"value_expr": {"col": "orders.id"}},
        {"value_subquery": {"from": "customers", "select": ["customers.id"]}},
    ],
)
@pytest.mark.parametrize("path", ["write/preview", "write/execute"])
async def test_rest_refuses_a_read_only_predicate_field_in_a_write(
    sqlite_app, read_only_field, path
):
    _enable_writes()
    body = {
        "op": "delete",
        "table": "orders",
        "where": {"col": "orders.id", "op": "eq", "value": 1, **read_only_field},
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(f"/api/v1/demo/{path}", json=body)
    assert resp.status_code in (400, 422), resp.text
    text = resp.text
    # Clean rejection: no server internals. (Echoing the caller's OWN field name
    # back is correct — it is how they learn which field was refused — and
    # test_malformed_input_fuzzing.py's token list deliberately treats echoed
    # input as non-sensitive.)
    for leak in ("Traceback", "sqlalchemy", "site-packages", "/Users/", "asyncpg"):
        assert leak not in text, f"{leak} leaked into the rejection: {text[:300]}"
    assert "not permitted" in text
    # The item-114 property: the error must never OFFER a removed field as an
    # alternative. The read predicate's own message does exactly that
    # ("requires a value, value_col, value_expr, or value_subquery"), which is why
    # WritePredicate re-raises in the write vocabulary.
    for offered in (
        "value_col, value_expr",
        "'value_col', 'value_expr'",
        "'col_fn', or 'expr'",
    ):
        assert offered not in text, f"the rejection offers a removed field: {text[:300]}"


@pytest.mark.asyncio
async def test_rest_still_accepts_a_boolean_group_write_filter(sqlite_app):
    """The narrowing must not have cost the real contract: an and/or/not filter is
    still accepted and previewed over the wire."""
    _enable_writes()
    body = {
        "op": "delete",
        "table": "orders",
        "where": {
            "and": [
                {"col": "orders.id", "op": "gt", "value": 0},
                {"or": [{"col": "orders.status", "op": "eq", "value": "completed"}]},
            ]
        },
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/write/preview", json=body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["executed"] is False
    assert payload["affected_rows"] >= 1
