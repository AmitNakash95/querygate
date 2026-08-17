"""Shared fixtures for all tests.

Every test gets an isolated in-memory connection registry, policy store,
catalog store (a "demo" connection with a permissive default policy and no
curated catalog), and a config-governance version store rooted in a fresh
tmp_path, so tests never depend on load order, real config files, or leak
state between each other.
"""

from __future__ import annotations

import pathlib

import pytest

# `pytester` is a first-party pytest plugin bundled with pytest itself (no new
# dependency) but disabled unless a top-level conftest opts in — it's what
# lets test_conftest_tier_markers.py run a real nested pytest process to pin
# this file's `tryfirst` hook-ordering guarantee end-to-end, not just the
# marker-assignment logic.
pytest_plugins = ["pytester"]

_TESTS_ROOT = pathlib.Path(__file__).parent
_TIER_DIRS = {"unit", "integration", "security"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every collected test with its directory tier (unit/integration/
    security), regardless of whether the file opted in with an explicit
    `pytestmark`/`@pytest.mark.<tier>`.

    `pytest -m unit` (and the `git commit` pre-commit gate that runs it) only
    ever saw whichever files someone remembered to mark — TODO.md item 124
    measured 52 of 78 `tests/unit/` files with no marker at all, invisible to
    that gate. Deriving the marker from the directory removes the failure mode
    for new files too, instead of just backfilling the missing 52. This must
    run before pytest's own `-m` deselection hook (also registered as
    `pytest_collection_modifyitems`) reads the markers, or the additions
    arrive too late to affect selection; `tryfirst` pins that ordering rather
    than leaving it to conftest-hook registration order, which already
    happens to run first by default but isn't a documented guarantee. A test
    that already carries the tier marker explicitly is untouched.
    """
    for item in items:
        try:
            tier = item.path.relative_to(_TESTS_ROOT).parts[0]
        except ValueError:
            continue
        if tier in _TIER_DIRS and tier not in {m.name for m in item.iter_markers()}:
            item.add_marker(getattr(pytest.mark, tier))


# `AppConfig` reads the repo's ".env" by default (see core/config.py's
# `class Config: env_file = ".env"`), so `make run`/`make run-dev` work with
# zero extra setup for local dev. That must not leak into the test suite: a
# developer's local .env (real API keys, SEMANTIC_MEMORY_PROVIDER,
# CATALOG_FILE, etc., per .env.example) would silently override the
# defaults tests construct and assert against. This has to happen before
# the first `querygate.core.config` import anywhere (its module-level
# `config` singleton is built at import time), so it runs before every
# other import in this file, including the ones below.
import querygate.core.config as _config_module  # noqa: E402

_config_module.AppConfig.model_config["env_file"] = None
_config_module.config = _config_module.AppConfig()

from querygate.admin.observed_shapes import (
    clear_redis_observed_shape_store,
    in_process_observed_shape_store,
)
from querygate.admin.store import ConfigVersionStore, set_config_version_store
from querygate.audit.sinks import reset_audit_sink
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.audit.worm_sink import reset_worm_buffer
from querygate.catalog.usage import reset_usage_signal_buffer
from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.async_execution import async_execution_store
from querygate.execution.concurrency import clear_redis_limiter, in_process_limiter
from querygate.execution.disclosure_budget import (
    clear_redis_disclosure_budget_limiter,
    in_process_disclosure_budget_limiter,
)
from querygate.execution.quota import clear_redis_quota_limiter, in_process_quota_limiter
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.templates.loader import TemplateStore, set_template_store
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
def reset_state(tmp_path):
    reset_audit_sink()
    set_registry(make_demo_registry())
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    set_catalog_store(CatalogStore.empty())
    set_template_store(TemplateStore.empty())
    set_config_version_store(ConfigVersionStore(str(tmp_path / "config_versions")))
    reset_engines()
    # asyncio.Semaphore objects are bound to the event loop that created
    # them; pytest-asyncio gives each test its own loop, so a semaphore
    # cached from a previous test would raise "bound to a different event
    # loop" (or, worse, look permanently "locked") if reused here.
    in_process_limiter().clear()
    clear_redis_limiter()
    in_process_quota_limiter().clear()
    clear_redis_quota_limiter()
    in_process_disclosure_budget_limiter().clear()
    clear_redis_disclosure_budget_limiter()
    reset_usage_signal_buffer()
    reset_worm_buffer()
    async_execution_store().clear()
    in_process_observed_shape_store().clear_sync()
    clear_redis_observed_shape_store()
    yield
    reset_audit_sink()
    reset_engines()
    in_process_limiter().clear()
    clear_redis_limiter()
    in_process_quota_limiter().clear()
    clear_redis_quota_limiter()
    in_process_disclosure_budget_limiter().clear()
    clear_redis_disclosure_budget_limiter()
    reset_usage_signal_buffer()
    reset_worm_buffer()
    async_execution_store().clear()
    in_process_observed_shape_store().clear_sync()
    clear_redis_observed_shape_store()
