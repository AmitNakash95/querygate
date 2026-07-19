"""Cross-process-safe atomic persistence for the versioned YAML catalog."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import weakref
from pathlib import Path
from typing import Awaitable, Callable, Generic, TextIO, TypeVar

import yaml

from querygate.catalog.loader import CatalogStore, set_catalog_store

T = TypeVar("T")

_PROCESS_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def catalog_process_lock() -> asyncio.Lock:
    """Per-event-loop transaction lock shared by refresh and config reload."""

    loop = asyncio.get_running_loop()
    lock = _PROCESS_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _PROCESS_LOCKS[loop] = lock
    return lock


class CatalogFileUpdate(Generic[T]):
    def __init__(self, store: CatalogStore, result: T) -> None:
        self.store = store
        self.result = result


class CatalogFileRepository:
    """Serialize read-modify-write operations with an adjacent file lock."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Catalog file not found: {path}")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def update(self, updater: Callable[[CatalogStore], CatalogFileUpdate[T]]) -> T:
        with self._acquire_lock():
            current = CatalogStore.from_file(str(self.path))
            update = updater(current)
            if update.store.to_dict() != current.to_dict():
                self._write_atomic(update.store)
            set_catalog_store(update.store)
            return update.result

    async def update_async(
        self,
        updater: Callable[[CatalogStore], Awaitable[CatalogFileUpdate[T]]],
    ) -> T:
        """Hold the cross-process lock across an asynchronous refresh scan."""

        async with catalog_process_lock():
            lock_file = await asyncio.to_thread(self._acquire_lock)
            try:
                current = CatalogStore.from_file(str(self.path))
                update = await updater(current)
                if update.store.to_dict() != current.to_dict():
                    # This bounded fsync/replace is short; keeping it on this task
                    # avoids thread-pool starvation when many other refresh tasks
                    # are blocked acquiring the same catalog lock.
                    self._write_atomic(update.store)
                set_catalog_store(update.store)
                return update.result
            finally:
                lock_file.close()

    def _acquire_lock(self) -> TextIO:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            lock_file = os.fdopen(lock_fd, "r+")
            self._lock(lock_file)
            return lock_file
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _lock(lock_file: TextIO) -> None:
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write("0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return

        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

    def _write_atomic(self, store: CatalogStore) -> None:
        payload = yaml.safe_dump(
            store.to_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        existing_mode = stat.S_IMODE(self.path.stat().st_mode)
        temp_fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            os.fchmod(temp_fd, existing_mode)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self.path)
            if os.name != "nt":
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.close(temp_fd)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
