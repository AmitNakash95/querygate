"""Pluggable persisted audit sinks."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol

from querygate.audit.events import PersistableEvent


class AuditSink(Protocol):
    def emit(self, event: PersistableEvent) -> None:
        """Persist one event or raise when persistence fails."""
        ...

    def close(self) -> None:
        """Release sink resources."""
        ...


class NullAuditSink:
    def emit(self, event: PersistableEvent) -> None:
        return None

    def close(self) -> None:
        return None


class JsonlAuditSink:
    """Append one JSON object per line with restrictive create permissions.

    The file is opened for every event rather than held indefinitely. This
    makes standard rename-and-recreate rotation work without signalling the
    process. O_APPEND plus one os.write call prevents threads in this process
    from overwriting one another; the lock also protects optional fsync.
    """

    def __init__(self, path: str, *, fsync: bool = False) -> None:
        if not path.strip():
            raise ValueError("AUDIT_JSONL_PATH must be set when AUDIT_SINK_BACKEND=jsonl")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()

    def emit(self, event: PersistableEvent) -> None:
        payload = (event.model_dump_json(exclude_none=True) + "\n").encode("utf-8")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        with self._lock:
            fd = os.open(self.path, flags, 0o600)
            try:
                os.write(fd, payload)
                if self._fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)

    def close(self) -> None:
        # The sink deliberately holds no open descriptor between events.
        return None


_sink: AuditSink = NullAuditSink()
_sink_lock = threading.Lock()


def get_audit_sink() -> AuditSink:
    return _sink


def set_audit_sink(sink: AuditSink) -> None:
    global _sink
    with _sink_lock:
        previous = _sink
        _sink = sink
    previous.close()


def configure_audit_sink(*, backend: str, jsonl_path: str, fsync: bool = False) -> None:
    if backend == "none":
        set_audit_sink(NullAuditSink())
        return
    if backend == "jsonl":
        set_audit_sink(JsonlAuditSink(jsonl_path, fsync=fsync))
        return
    raise ValueError(f"Unsupported audit sink backend: {backend!r}")


def reset_audit_sink() -> None:
    set_audit_sink(NullAuditSink())
