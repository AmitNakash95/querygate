"""Pluggable persisted audit sinks."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional, Protocol

from querygate.audit.events import PersistableEvent
from querygate.audit.ledger import (
    GENESIS_PREV_HASH,
    LedgerRecord,
    make_record,
)


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


def _read_last_line(path: Path) -> Optional[str]:
    """Return the last non-empty line of a file, reading only its tail.

    Used to recover a hash chain's head on startup without scanning the whole
    (potentially large) ledger. Reads a bounded window from the end, expanding
    only if no newline is found yet — chain records are a few hundred bytes, so
    one window is effectively always enough.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    window = 65536
    with path.open("rb") as handle:
        pos = size
        chunk = b""
        while pos > 0:
            step = min(window, pos)
            pos -= step
            handle.seek(pos)
            chunk = handle.read(size - pos)
            # Need at least one newline *before* the trailing content to isolate
            # a whole last line; keep expanding toward the start otherwise.
            if chunk.strip(b"\n").count(b"\n") >= 1 or pos == 0:
                break
    text = chunk.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return None


class HashChainedAuditSink:
    """Append-only, tamper-evident JSONL ledger (TODO.md item 91, F5).

    Wraps every persisted event in a hash-chain envelope (`LedgerRecord`) that
    links it to the previous record's hash, so any later edit/deletion/reorder/
    insertion is detectable by `querygate-audit verify`. The embedded ``event``
    is byte-for-byte the redaction-safe body the plain JSONL sink writes — the
    chain adds only a sequence number and hashes, never customer data.

    When ``key`` is set the chain uses HMAC-SHA256 (forgery-resistant against a
    writer with file access); otherwise SHA-256 (integrity/ordering, tamper-
    evident relative to an externally anchored head). One logical writer owns the
    head — within a process a lock serializes writes; across replicas each writer
    needs its own ledger file.
    """

    def __init__(self, path: str, *, key: Optional[bytes] = None, fsync: bool = False) -> None:
        if not path.strip():
            raise ValueError("AUDIT_JSONL_PATH must be set when AUDIT_SINK_BACKEND=jsonl_chained")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = key or None
        self._fsync = fsync
        self._lock = threading.Lock()
        self._next_seq, self._head_hash = self._recover_head()

    def _recover_head(self) -> tuple[int, str]:
        """Resume the chain from an existing ledger file, or start at genesis."""
        last = _read_last_line(self.path)
        if last is None:
            return 0, GENESIS_PREV_HASH
        try:
            record = LedgerRecord.model_validate_json(last)
        except ValueError as exc:
            raise ValueError(
                f"Cannot resume hash-chained audit ledger {self.path}: its last "
                f"line is not a valid ledger record ({exc}). Refusing to append "
                "and silently fork the chain."
            ) from exc
        return record.seq + 1, record.hash

    def emit(self, event: PersistableEvent) -> None:
        body = event.model_dump(mode="json", exclude_none=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        with self._lock:
            record = make_record(self._next_seq, self._head_hash, body, key=self._key)
            payload = (record.model_dump_json() + "\n").encode("utf-8")
            fd = os.open(self.path, flags, 0o600)
            try:
                os.write(fd, payload)
                if self._fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)
            # Only advance the in-memory head after a successful write, so a
            # write failure doesn't leave the chain pointing past a record that
            # was never persisted.
            self._next_seq = record.seq + 1
            self._head_hash = record.hash

    @property
    def head_hash(self) -> str:
        """The current chain head — anchor it externally to detect tail truncation."""
        return self._head_hash

    def close(self) -> None:
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


def configure_audit_sink(
    *,
    backend: str,
    jsonl_path: str,
    fsync: bool = False,
    ledger_hmac_key: str = "",
) -> None:
    if backend == "none":
        set_audit_sink(NullAuditSink())
        return
    if backend == "jsonl":
        set_audit_sink(JsonlAuditSink(jsonl_path, fsync=fsync))
        return
    if backend == "jsonl_chained":
        key = ledger_hmac_key.encode("utf-8") if ledger_hmac_key.strip() else None
        set_audit_sink(HashChainedAuditSink(jsonl_path, key=key, fsync=fsync))
        return
    raise ValueError(f"Unsupported audit sink backend: {backend!r}")


def reset_audit_sink() -> None:
    set_audit_sink(NullAuditSink())
