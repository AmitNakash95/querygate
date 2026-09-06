"""Hot-reload connections.yaml / policy.yaml / catalog.yaml without a process restart.

`connections/registry.py`, `policy/loader.py`, and `catalog/loader.py` each
load their YAML file once into a process-wide singleton on first access —
tightening a policy in response to an incident, or adding a connection,
previously required a full redeploy. `reload_config()` rebuilds every store
from disk and swaps them in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

import pydantic as pyd
import yaml

from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.repository import catalog_process_lock
from querygate.connections.engine import dispose_engine
from querygate.connections.registry import ConnectionRegistry, get_registry, set_registry
from querygate.core.config import AppConfig
from querygate.core.logging import get_logger
from querygate.execution.concurrency import in_process_limiter
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.identity.discovery import clear_discovery_cache
from querygate.identity.local_store import LocalUserStore, set_local_user_store
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.secrets.resolvers import SecretResolverRegistry, build_secret_resolver_registry
from querygate.templates.loader import TemplateStore, set_template_store


class ReloadResult(pyd.BaseModel):
    connection_ids: List[str]
    policy_connection_overrides: List[str]
    catalog_connection_ids: List[str]
    template_ids: List[str]
    disposed_connections: List[str]


async def reload_config(
    *,
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    template_file: Optional[str] = None,
    identity_file: Optional[str] = None,
    local_users_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
) -> ReloadResult:
    """Serialize a full config swap against in-process catalog refresh."""

    async with catalog_process_lock():
        return await _reload_config_unlocked(
            connections_file=connections_file,
            policy_file=policy_file,
            catalog_file=catalog_file,
            template_file=template_file,
            identity_file=identity_file,
            local_users_file=local_users_file,
            resolver_registry=resolver_registry,
        )


async def _reload_config_unlocked(
    *,
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    template_file: Optional[str] = None,
    identity_file: Optional[str] = None,
    local_users_file: Optional[str] = None,
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
    new_template_store = (
        TemplateStore.from_file(template_file) if template_file else TemplateStore.empty()
    )
    # Identity (item 199) reloads with everything else, which is the point:
    # tightening a claim→scope rule is an incident response, and it must not
    # need a restart. It takes effect on the *next request* even for callers
    # already signed in, because `identity/authenticators.py` re-derives scopes
    # from this store per request rather than freezing them at login.
    #
    # `identity_file=None` means **leave identity alone**, deliberately unlike
    # `catalog_file`/`template_file` (where None means "empty"). Identity is not
    # a governed, version-snapshotted document, so every caller that reloads for
    # some other reason — a config-governance apply, a leased-credential refresh
    # — would otherwise silently wipe every provider and mapping rule out of the
    # running process, locking every signed-in human out. Not-passed must mean
    # not-touched.
    new_identity_store = (
        IdentityConfigStore.from_file(identity_file, resolver_registry=resolver_registry)
        if identity_file
        else None
    )
    new_local_user_store = (
        LocalUserStore.from_file(local_users_file)
        if local_users_file and Path(local_users_file).exists()
        else None
    )

    set_registry(new_registry)
    set_policy_store(new_policy_store)
    set_catalog_store(new_catalog_store)
    set_template_store(new_template_store)
    if new_identity_store is not None:
        set_identity_store(new_identity_store)
        # An IdP can rotate an endpoint or a signing key at any time; reloading
        # identity is the operator's explicit "pick up the new configuration"
        # signal, so the cached discovery documents go with it rather than
        # lingering for their TTL.
        clear_discovery_cache()
    if new_local_user_store is not None:
        set_local_user_store(new_local_user_store)

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
        template_ids=new_template_store.template_ids(),
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


def _connection_string_reference_text(connections_file: str) -> str:
    """Only the text `SecretResolverRegistry.interpolate` will actually
    resolve on a real reload — each entry's `connection_string` field
    (mirrors `ConnectionRegistry.from_entries`'s own traversal) — not the
    whole raw file. A `${...}`-shaped reference sitting in a comment or an
    unrelated key is never resolved by `reload_config()`, so probing it for
    a lease (and potentially triggering a reload because of it) would be
    reacting to something reload never reads.
    """
    raw = yaml.safe_load(Path(connections_file).read_text()) or {}
    return "\n".join(
        str(entry.get("connection_string", "")) for entry in raw.get("connections", [])
    )


def _current_source_paths(
    cfg: AppConfig,
) -> "tuple[str, str, Optional[str], Optional[str]]":
    """The files a reload should actually read from *right now*.

    `POST /admin/reload-config` always reads the plain `cfg.*` paths. But
    `admin/service.py`'s config-governance `apply()`/rollback path reloads
    from a staged version's own on-disk copy under
    `AppConfig.config_governance_dir` (`ConfigVersionStore.file_paths`) and
    never writes that approved content back to `cfg.connections_file` et al.
    A background trigger that always used the plain `cfg.*` paths would,
    the moment a lease came due in any deployment using config governance,
    silently revert the live registry/policy back to the stale on-disk
    documents a four-eyes-approved version had already superseded — with no
    audit trail, since it isn't a governance action. Prefer the active
    governed version's files when one exists; fall back to the plain
    `cfg.*` paths otherwise (governance API never used).
    """
    from querygate.admin.store import get_config_version_store

    store = get_config_version_store()
    active_id = store.get_active_version_id()
    if active_id is None:
        return cfg.connections_file, cfg.policy_file, cfg.catalog_file, cfg.template_file
    paths = store.file_paths(active_id)
    # A version staged before templates became governed (item 48 phase 2)
    # has no template snapshot -- fall back to the deployment's static
    # template file rather than clobbering it, exactly mirroring
    # admin/service.py's own apply()/rollback().
    template_file = paths.templates if paths.templates is not None else cfg.template_file
    return paths.connections, paths.policy, paths.catalog, template_file


class _ProactiveRefreshCheck(NamedTuple):
    due: bool
    expires_at: Optional[datetime]
    connections_file: str
    policy_file: str
    catalog_file: Optional[str]
    template_file: Optional[str]


class CredentialLeaseMonitor:
    """Proactively triggers `reload_config()` before a leased credential's
    TTL expires, instead of waiting for an operator to call
    `POST /admin/reload-config` (TODO.md item 135; item 13 shipped the
    reload/dispose machinery this reuses verbatim).

    Independent, fail-open background task, disabled unless explicitly
    enabled — mirrors `catalog/refresh.py`'s `CatalogRefreshMonitor` shape:
    an `asyncio.Event`-gated poll loop, a catch-all around each iteration so
    one failed probe or reload never kills the loop, and only the exception
    type (never its message) logged, since a driver/Vault error can carry a
    credential or raw server text.

    Each poll re-resolves the *currently live* source files
    (`_current_source_paths` — governance-aware, see its docstring) and
    probes every `${scheme:reference}` a real reload would actually resolve
    (`_connection_string_reference_text`) whose resolver implements
    `LeasedSecretResolver` (`secrets/resolvers.py`) for its current lease
    expiry via `SecretResolverRegistry.soonest_lease_expiry`. When the
    soonest reported expiry is within `refresh_margin_seconds` *and* hasn't
    already been acted on (`_last_triggered_expiry` — without this, a
    connection whose lease TTL is shorter than the margin, the normal case
    for a real dynamic credential, would trigger a reload on every single
    poll forever), it calls this module's own `reload_config()` — the exact
    same registry/policy swap and `_dispose_stale_engines` diffing the
    operator-triggered path already uses, with the same in-flight-safety
    `connections/engine.dispose_engine` documents — and records an
    `audit_config_change(action="lease_refresh", ...)` event so an automatic
    change this monitor makes is attributable, same as any other config
    change. A reference whose resolver has no lease to report (env, a
    static Vault KV v2 secret) never triggers a reload from this monitor —
    it stays reachable only through the existing operator-pull path.

    The probe (file read + a real Vault network call for a leased
    reference) runs off the event loop via `asyncio.to_thread`, so a
    slow/partitioned Vault stalls a thread-pool worker, not every other
    concurrent request this process is serving.

    `resolver_registry_factory` is called fresh on every poll (default:
    `build_secret_resolver_registry(cfg)`) rather than once at construction
    — a rotated `VAULT_TOKEN` is picked up by `/admin/reload-config` and
    config-governance apply (both rebuild their registry per call); a
    monitor pinning one registry for its whole process lifetime would keep
    using a stale token and fail every poll once it expired.
    """

    def __init__(
        self,
        *,
        cfg: AppConfig,
        poll_interval_seconds: float,
        refresh_margin_seconds: float,
        resolver_registry_factory: Optional[Callable[[], SecretResolverRegistry]] = None,
    ) -> None:
        self._cfg = cfg
        self._resolver_registry_factory = resolver_registry_factory or (
            lambda: build_secret_resolver_registry(cfg)
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._refresh_margin_seconds = refresh_margin_seconds
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_triggered_expiry: Optional[datetime] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    def _evaluate(self) -> _ProactiveRefreshCheck:
        """Sync and deliberately run off the event loop (`_run` calls this
        via `asyncio.to_thread`) — probing a leased resolver can be a real,
        potentially slow network call.
        """
        connections_file, policy_file, catalog_file, template_file = _current_source_paths(
            self._cfg
        )
        text = _connection_string_reference_text(connections_file)
        registry = self._resolver_registry_factory()
        expires_at = registry.soonest_lease_expiry(text)
        due = (
            expires_at is not None
            and expires_at != self._last_triggered_expiry
            and expires_at
            <= datetime.now(timezone.utc) + timedelta(seconds=self._refresh_margin_seconds)
        )
        return _ProactiveRefreshCheck(
            due=due,
            expires_at=expires_at,
            connections_file=connections_file,
            policy_file=policy_file,
            catalog_file=catalog_file,
            template_file=template_file,
        )

    async def _run(self) -> None:
        from querygate.audit.logger import audit_config_change

        log = get_logger()
        while not self._stop.is_set():
            try:
                check = await asyncio.to_thread(self._evaluate)
                if check.due:
                    result = await reload_config(
                        connections_file=check.connections_file,
                        policy_file=check.policy_file,
                        catalog_file=check.catalog_file,
                        template_file=check.template_file,
                        resolver_registry=self._resolver_registry_factory(),
                    )
                    self._last_triggered_expiry = check.expires_at
                    log.info(
                        "secrets.lease_refresh_triggered",
                        disposed_connections=result.disposed_connections,
                    )
                    audit_config_change(
                        action="lease_refresh",
                        outcome="success",
                        principal="system:credential-lease-monitor",
                        auth_method="background_monitor",
                        description=(
                            "Proactive credential re-resolution triggered before a "
                            "leased reference's TTL expired."
                        ),
                        duration_ms=0,
                    )
            except Exception as exc:
                # Same posture as CatalogRefreshMonitor/CatalogUsageLearningMonitor:
                # only the exception type is logged, never its message, and one
                # bad iteration never kills the loop.
                log.error("secrets.lease_refresh_failed", error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
