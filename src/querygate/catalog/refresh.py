"""Opt-in row-free schema refresh and selective catalog invalidation."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import CatalogDraftObjectType, CatalogEntryStatus
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.catalog.schema_memory import (
    ObservedSchemaSnapshot,
    SchemaDiff,
    diff_schema_snapshots,
)
from querygate.connections.engine import get_engine
from querygate.connections.registry import get_registry
from querygate.core.logging import get_logger
from querygate.schema.reflection import get_table_schema, list_live_tables


@dataclass(frozen=True)
class CatalogRefreshUpdate:
    store: CatalogStore
    connection_id: str
    before_fingerprint: Optional[str]
    after_fingerprint: str
    diff: Optional[SchemaDiff]
    stale_entry_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.before_fingerprint != self.after_fingerprint


def _changed_objects(diff: SchemaDiff) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    removed_tables: set[str] = set()
    changed_table_entries: set[str] = set()
    changed_columns: set[tuple[str, str]] = set()
    relationship_tables: set[str] = set()
    for change in diff.changes:
        table = change.table.casefold()
        if change.kind == "table_removed":
            removed_tables.add(table)
        elif change.kind in {"comment_changed", "indexes_changed"}:
            changed_table_entries.add(table)
        elif change.kind in {"column_removed", "column_changed"} and change.column:
            changed_columns.add((table, change.column.casefold()))
        elif change.kind == "relationships_changed":
            relationship_tables.add(table)
    return (
        removed_tables,
        changed_table_entries,
        changed_columns | {(table, "*") for table in relationship_tables},
    )


def _relationship_affected(
    *,
    from_table: str,
    column: str,
    to_table: str,
    to_column: str,
    removed_tables: set[str],
    changed_columns: set[tuple[str, str]],
) -> bool:
    source = from_table.casefold()
    target = to_table.casefold()
    return (
        source in removed_tables
        or target in removed_tables
        or (source, "*") in changed_columns
        or (source, column.casefold()) in changed_columns
        or (target, to_column.casefold()) in changed_columns
    )


def _mark_or_rebind(provenance: dict, *, affected: bool, fingerprint: str) -> Optional[str]:
    status = provenance.get("status", "verified")
    if status in {CatalogEntryStatus.REJECTED.value, CatalogEntryStatus.ARCHIVED.value}:
        return None
    if affected:
        if status != CatalogEntryStatus.STALE.value:
            provenance["status"] = CatalogEntryStatus.STALE.value
            return provenance.get("entry_id")
        return None
    if status != CatalogEntryStatus.STALE.value:
        provenance["schema_fingerprint"] = fingerprint
    return None


def _aggregation_references_changed_column(
    aggregation: Optional[str], table: str, changed_columns: set[tuple[str, str]]
) -> bool:
    if not aggregation:
        return False
    tokens = {token.casefold() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", aggregation)}
    return any(
        changed_table == table.casefold() and column != "*" and column in tokens
        for changed_table, column in changed_columns
    )


def refresh_catalog_schema(
    store: CatalogStore, snapshot: ObservedSchemaSnapshot
) -> CatalogRefreshUpdate:
    """Persist one snapshot and stale only entries its structured diff affects."""

    connection_id = snapshot.connection_id
    previous = store.get_schema_snapshot(connection_id)
    if previous is not None and previous.fingerprint == snapshot.fingerprint:
        return CatalogRefreshUpdate(
            store=store,
            connection_id=connection_id,
            before_fingerprint=previous.fingerprint,
            after_fingerprint=snapshot.fingerprint,
            diff=diff_schema_snapshots(previous, snapshot),
        )

    diff = diff_schema_snapshots(previous, snapshot) if previous is not None else None
    removed_tables: set[str] = set()
    changed_table_entries: set[str] = set()
    changed_columns: set[tuple[str, str]] = set()
    if diff is not None:
        removed_tables, changed_table_entries, changed_columns = _changed_objects(diff)

    raw = store.to_dict()
    if raw.get("version") == 1:
        raw["version"] = 2
        for catalog_connection in raw.get("connections", {}).values():
            for catalog_table in catalog_connection.get("tables", {}).values():
                catalog_table["provenance"]["catalog_version"] = 2
                for catalog_column in catalog_table.get("columns", {}).values():
                    catalog_column["provenance"]["catalog_version"] = 2
                for catalog_relationship in catalog_table.get("relationships", []):
                    catalog_relationship["provenance"]["catalog_version"] = 2
    stale_ids: list[str] = []
    connection = raw.get("connections", {}).get(connection_id, {})
    for table_name, table in connection.get("tables", {}).items():
        table_key = table_name.casefold()
        table_affected = (
            table_key in removed_tables
            or table_key in changed_table_entries
            or _aggregation_references_changed_column(
                table.get("default_aggregation"), table_name, changed_columns
            )
        )
        stale_id = _mark_or_rebind(
            table["provenance"], affected=table_affected, fingerprint=snapshot.fingerprint
        )
        if stale_id:
            stale_ids.append(stale_id)
        for column_name, column in table.get("columns", {}).items():
            affected = (
                table_key in removed_tables
                or (
                    table_key,
                    column_name.casefold(),
                )
                in changed_columns
            )
            stale_id = _mark_or_rebind(
                column["provenance"], affected=affected, fingerprint=snapshot.fingerprint
            )
            if stale_id:
                stale_ids.append(stale_id)
        for relationship in table.get("relationships", []):
            affected = _relationship_affected(
                from_table=table_name,
                column=relationship["column"],
                to_table=relationship["to_table"],
                to_column=relationship["to_column"],
                removed_tables=removed_tables,
                changed_columns=changed_columns,
            )
            stale_id = _mark_or_rebind(
                relationship["provenance"],
                affected=affected,
                fingerprint=snapshot.fingerprint,
            )
            if stale_id:
                stale_ids.append(stale_id)

    for proposal in raw.get("draft_proposals", []):
        target = proposal["target"]
        if target["connection_id"] != connection_id:
            continue
        table_key = target["table"].casefold()
        object_type = CatalogDraftObjectType(target["object_type"])
        if object_type == CatalogDraftObjectType.TABLE:
            affected = table_key in removed_tables or table_key in changed_table_entries
        elif object_type == CatalogDraftObjectType.COLUMN:
            affected = (
                table_key in removed_tables
                or (
                    table_key,
                    target["column"].casefold(),
                )
                in changed_columns
            )
        else:
            affected = _relationship_affected(
                from_table=target["table"],
                column=target["column"],
                to_table=target["to_table"],
                to_column=target["to_column"],
                removed_tables=removed_tables,
                changed_columns=changed_columns,
            )
        stale_id = _mark_or_rebind(
            proposal["provenance"], affected=affected, fingerprint=snapshot.fingerprint
        )
        if stale_id:
            stale_ids.append(stale_id)

    raw.setdefault("schema_snapshots", {})[connection_id] = snapshot.model_dump(mode="json")
    return CatalogRefreshUpdate(
        store=store.replace(raw),
        connection_id=connection_id,
        before_fingerprint=previous.fingerprint if previous is not None else None,
        after_fingerprint=snapshot.fingerprint,
        diff=diff,
        stale_entry_ids=tuple(sorted(set(stale_ids))),
    )


async def scan_connection_schema(
    connection_id: str, *, max_tables: int = 500
) -> ObservedSchemaSnapshot:
    """Reflect a bounded schema without selecting or persisting any row values."""

    # ``known_tables`` is only a discovery seed and may be intentionally
    # incomplete. A drift snapshot must enumerate the live schema or fail;
    # falling back to a partial seed could falsely stale unseen objects.
    get_registry().get(connection_id)
    table_names = await list_live_tables(connection_id)
    unique_names = sorted(set(table_names), key=lambda value: (value.casefold(), value))
    if len(unique_names) > max_tables:
        raise ValueError("schema refresh exceeds the configured table limit")
    engine = get_engine(connection_id)
    tables = [
        await get_table_schema(table_name, connection_id, engine) for table_name in unique_names
    ]
    return ObservedSchemaSnapshot.from_tables(connection_id, tables)


class CatalogRefreshMonitor:
    """Independent fail-open background refresh; disabled unless explicitly enabled."""

    def __init__(
        self,
        *,
        catalog_file: str,
        interval_seconds: float,
        max_tables: int = 500,
    ) -> None:
        self._repository = CatalogFileRepository(catalog_file)
        self._interval = interval_seconds
        self._max_tables = max_tables
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        connection_ids = [
            connection.id for connection in get_registry().list_public() if connection.enabled
        ]
        self._tasks = [
            asyncio.create_task(self._run_connection(connection_id))
            for connection_id in connection_ids
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks)
        self._tasks = []

    async def refresh_once(self, connection_id: str) -> CatalogRefreshUpdate:
        async def _apply(store: CatalogStore) -> CatalogFileUpdate[CatalogRefreshUpdate]:
            snapshot = await scan_connection_schema(connection_id, max_tables=self._max_tables)
            update = refresh_catalog_schema(store, snapshot)
            return CatalogFileUpdate(update.store, update)

        return await self._repository.update_async(_apply)

    async def _run_connection(self, connection_id: str) -> None:
        log = get_logger()
        while not self._stop.is_set():
            try:
                update = await self.refresh_once(connection_id)
                log.info(
                    "semantic_memory.schema_refresh",
                    connection=connection_id,
                    changed=update.changed,
                    stale_entry_count=len(update.stale_entry_ids),
                    schema_fingerprint=update.after_fingerprint,
                )
            except Exception as exc:
                # Driver errors can contain credentials or raw server text.
                log.error(
                    "semantic_memory.schema_refresh_failed",
                    connection=connection_id,
                    error_type=type(exc).__name__,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
