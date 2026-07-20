"""Hot-reload connections.yaml / policy.yaml / catalog.yaml without a process restart.

`connections/registry.py`, `policy/loader.py`, and `catalog/loader.py` each
load their YAML file once into a process-wide singleton on first access —
tightening a policy in response to an incident, or adding a connection,
previously required a full redeploy. `reload_config()` rebuilds every store
from disk and swaps them in.
"""

from __future__ import annotations

from typing import List, Optional

import pydantic as pyd

from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.repository import catalog_process_lock
from querygate.connections.engine import dispose_engine
from querygate.connections.registry import ConnectionRegistry, get_registry, set_registry
from querygate.core.logging import get_logger
from querygate.execution.concurrency import in_process_limiter
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.secrets.resolvers import SecretResolverRegistry


class ReloadResult(pyd.BaseModel):
    connection_ids: List[str]
    policy_connection_overrides: List[str]
    catalog_connection_ids: List[str]
    disposed_connections: List[str]


async def reload_config(
    *,
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
) -> ReloadResult:
    """Serialize a full config swap against in-process catalog refresh."""

    async with catalog_process_lock():
        return await _reload_config_unlocked(
            connections_file=connections_file,
            policy_file=policy_file,
            catalog_file=catalog_file,
            resolver_registry=resolver_registry,
        )


async def _reload_config_unlocked(
    *,
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
) -> ReloadResult:
    """Atomically swap in a freshly loaded registry + policy store.

    Safe for in-flight requests: `set_registry`/`set_policy_store` are a
    single reference assignment (atomic under the GIL), so a request that
    already read the old registry/policy keeps using it to completion — it
    never observes a half-swapped state.

    A connection that was removed, or whose `connection_string`/`dialect`
    changed, has its cached engine disposed (see
    `connections/engine.dispose_engine`) so the next use lazily reconnects
    with the new settings; an unchanged connection keeps its existing
    engine/pool to avoid unnecessary reconnect churn. Every connection's
    concurrency semaphore is reset unconditionally (not diffed), since it's
    the one piece of `Policy` baked into cached state
    (`execution/concurrency.py`'s `InProcessConcurrencyLimiter`) and reloads are rare, admin-
    triggered operations, not hot-path — a request already holding a permit
    on the old semaphore still releases it correctly; there's a brief
    transition window where a connection's observed concurrency can exceed
    either the old or new limit, an accepted tradeoff for a live reload over
    a hard cutover.

    `resolver_registry` (see `querygate/secrets/resolvers.py`) resolves any
    `${...}` reference in the reloaded connections file — env-only when
    omitted, matching `ConnectionRegistry.from_file`'s own default. Vault
    connectivity itself (`AppConfig.vault_*`) is process-level config and
    isn't part of what this function reloads; pass a registry built from the
    current `AppConfig` to resolve `${vault:...}` references during reload.
    """
    old_registry = get_registry()
    new_registry = ConnectionRegistry.from_file(
        connections_file, resolver_registry=resolver_registry
    )
    new_policy_store = PolicyStore.from_file(policy_file)
    new_catalog_store = (
        CatalogStore.from_file(catalog_file) if catalog_file else CatalogStore.empty()
    )

    set_registry(new_registry)
    set_policy_store(new_policy_store)
    set_catalog_store(new_catalog_store)

    disposed = await _dispose_stale_engines(old_registry, new_registry)
    for connection_id in new_registry.all_ids():
        in_process_limiter().reset_semaphore(connection_id)

    get_logger().info(
        "config.reload",
        connections=new_registry.all_ids(),
        disposed_connections=disposed,
    )
    return ReloadResult(
        connection_ids=new_registry.all_ids(),
        policy_connection_overrides=new_policy_store.override_connection_ids(),
        catalog_connection_ids=new_catalog_store.connection_ids(),
        disposed_connections=disposed,
    )


async def _dispose_stale_engines(old: ConnectionRegistry, new: ConnectionRegistry) -> List[str]:
    old_ids = set(old.all_ids())
    new_ids = set(new.all_ids())
    stale = old_ids - new_ids
    for connection_id in old_ids & new_ids:
        old_profile = old.get(connection_id)
        new_profile = new.get(connection_id)
        if (
            old_profile.connection_string != new_profile.connection_string
            or old_profile.dialect != new_profile.dialect
        ):
            stale.add(connection_id)
    for connection_id in stale:
        await dispose_engine(connection_id)
    return sorted(stale)
