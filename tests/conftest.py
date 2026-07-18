"""Shared fixtures for all tests.

Every test gets an isolated in-memory connection registry, policy store, and
catalog store (a "demo" connection with a permissive default policy and no
curated catalog) so tests never depend on load order, real config files, or
leak state between each other.
"""

from __future__ import annotations

import pytest

from querygate.audit.sinks import reset_audit_sink
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.concurrency import SEMAPHORES, clear_redis_limiter
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy


def make_demo_registry(**profile_overrides) -> ConnectionRegistry:
    defaults = dict(
        id="demo",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@localhost/demo",
        description="Test demo connection",
        known_tables=["customers", "orders", "order_items"],
    )
    defaults.update(profile_overrides)
    profile = ConnectionProfile(**defaults)
    return ConnectionRegistry({profile.id: profile})


@pytest.fixture(autouse=True)
def reset_state():
    reset_audit_sink()
    set_registry(make_demo_registry())
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    set_catalog_store(CatalogStore.empty())
    reset_engines()
    # asyncio.Semaphore objects are bound to the event loop that created
    # them; pytest-asyncio gives each test its own loop, so a semaphore
    # cached from a previous test would raise "bound to a different event
    # loop" (or, worse, look permanently "locked") if reused here.
    SEMAPHORES.clear()
    clear_redis_limiter()
    yield
    reset_audit_sink()
    reset_engines()
    SEMAPHORES.clear()
    clear_redis_limiter()
