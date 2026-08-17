"""Integration tests for TODO.md item 195's REST surface:
`GET /api/v1/admin/observability/observed-shapes` and its
`/{shape_hash}/template-draft` sibling.

What matters here beyond "the route works":

- both reads are gated by their **own** scope (`admin:shapes:read`), not by
  `admin:observability:read` — an observed shape names one principal's exact
  tables, columns and predicates, which is a materially more specific
  disclosure than the aggregate dashboards on the same router;
- the response carries no data values, so the admin API cannot become the leak
  the recorder itself was designed to avoid; and
- drafting returns a template and does **not** install it.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.admin.observed_shapes import (
    configure_observed_shape_store,
    observed_shape_store,
)
from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.query_ast.models import Predicate, StructuredQuery
from querygate.templates.loader import get_template_store

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "shapes-admin-key"
_SHAPES_SCOPE = "admin:shapes:read"
_OBS_SCOPE = "admin:observability:read"
_URL = "/api/v1/admin/observability/observed-shapes"


def _configure_store(enabled: bool) -> None:
    """The ASGI transport used here does not run the app lifespan, so the
    startup call to `configure_observed_shape_store` never fires — configure
    the store directly, matching what a real deployment's startup does.
    """
    configure_observed_shape_store(500, enabled=enabled)


def _settings(scopes, *, enabled=True, **overrides) -> AppConfig:
    kwargs = dict(
        environment="localhost",
        mcp_enabled=False,
        observed_shapes_enabled=enabled,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
    )
    kwargs.update(overrides)
    return AppConfig(**kwargs)


def _auth(key: str = _ADMIN_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get(app, path=_URL, params=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get(path, headers=_auth(), params=params or {})


async def _seed(value: str = "completed", *, enabled: bool = True) -> str:
    _configure_store(enabled=True)
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="eq", value=value),
        limit=5,
    )
    shape = await observed_shape_store().record(query, connection_id="demo", principal_id="agent")
    return shape.shape_hash


@pytest.mark.asyncio
async def test_requires_a_scope_at_all():
    app = create_app(_settings(()))
    assert (await _get(app)).status_code == 403


@pytest.mark.asyncio
async def test_the_observability_scope_alone_is_not_enough():
    """Holding the dashboard scope must not imply the ability to read one
    principal's exact query catalogue.
    """
    app = create_app(_settings((_OBS_SCOPE,)))
    assert (await _get(app)).status_code == 403


@pytest.mark.asyncio
async def test_lists_recorded_shapes_without_any_values():
    await _seed("a-secret-status")
    app = create_app(_settings((_SHAPES_SCOPE,)))
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["scope"] == "process-local-volatile"
    assert len(body["shapes"]) == 1
    # The whole point: the shape names the column, never the value.
    assert "orders.status" in str(body["shapes"][0]["skeleton"])
    assert "a-secret-status" not in resp.text


@pytest.mark.asyncio
async def test_reports_disabled_honestly_rather_than_as_an_empty_deployment():
    await _seed()
    app = create_app(_settings((_SHAPES_SCOPE,), enabled=False))
    observed_shape_store().enabled = False
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["shapes"] == []


@pytest.mark.asyncio
async def test_filters_by_connection_and_principal():
    await _seed()
    app = create_app(_settings((_SHAPES_SCOPE,)))
    assert len((await _get(app, params={"connection_id": "demo"})).json()["shapes"]) == 1
    assert len((await _get(app, params={"connection_id": "other"})).json()["shapes"]) == 0
    assert len((await _get(app, params={"principal_id": "nobody"})).json()["shapes"]) == 0


@pytest.mark.asyncio
async def test_drafts_a_template_without_installing_it():
    shape_hash = await _seed()
    app = create_app(_settings((_SHAPES_SCOPE,)))
    resp = await _get(
        app,
        path=f"{_URL}/{shape_hash}/template-draft",
        params={"template_id": "orders_by_status"},
    )
    assert resp.status_code == 200
    draft = resp.json()
    assert draft["id"] == "orders_by_status"
    assert draft["connection"] == "demo"
    # `limit` is parameterized too — every caller-supplied value is (see
    # observed_shapes._LITERAL_KEYS).
    assert "status" in [p["name"] for p in draft["parameters"]]
    # Returned for review — never added to the live template store, so the
    # human review gate cannot be skipped by calling this endpoint.
    assert get_template_store().get("orders_by_status") is None


@pytest.mark.asyncio
async def test_drafting_an_unknown_shape_is_a_404():
    app = create_app(_settings((_SHAPES_SCOPE,)))
    resp = await _get(
        app,
        path=f"{_URL}/deadbeef/template-draft",
        params={"template_id": "whatever"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_drafting_requires_the_shapes_scope():
    shape_hash = await _seed()
    app = create_app(_settings((_OBS_SCOPE,)))
    resp = await _get(
        app,
        path=f"{_URL}/{shape_hash}/template-draft",
        params={"template_id": "orders_by_status"},
    )
    assert resp.status_code == 403
