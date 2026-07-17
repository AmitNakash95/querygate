"""Structured JSON logging, built on loguru.

Kept intentionally small: one JSON-lines stdout sink plus a ContextLogger
that binds structured fields and reports start/success/failure. Callers
should never pass connection strings or full row payloads as log fields —
see querygate/audit/logger.py for the redaction-safe audit record shape.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator, Optional

from loguru import logger as _logger

APP_NAME = os.getenv("APP_NAME", "querygate")
ENVIRONMENT = os.getenv("ENVIRONMENT", "localhost")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_LOGGER_LOCK = threading.Lock()
_logger_initialized = False


def _json_sink(message: Any) -> None:
    record = message.record
    exc = record["exception"]
    entry: dict[str, Any] = {
        "time": record["time"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "level": record["level"].name,
        "app": APP_NAME,
        "environment": ENVIRONMENT,
        "message": record["message"],
    }
    if exc and exc.value:
        entry["exception"] = f"{type(exc.value).__name__}: {exc.value}"
    entry.update({k: v for k, v in record["extra"].items() if v is not None})
    sys.stdout.write(json.dumps(entry, default=str) + "\n")
    sys.stdout.flush()


def _init_logger() -> None:
    global _logger_initialized
    with _LOGGER_LOCK:
        if _logger_initialized:
            return
        _logger.remove()
        _logger.add(_json_sink, level=LOG_LEVEL, backtrace=False, diagnose=False)
        _logger_initialized = True


class ContextLogger:
    """Loguru wrapper that binds structured context and exposes lifecycle helpers."""

    __slots__ = ("extra", "_logger", "_start")

    def __init__(self, **extra: Any) -> None:
        _init_logger()
        self._start = time.perf_counter()
        self.extra = extra
        self._logger = _logger.bind(**extra)

    @contextmanager
    def contextualize(self, **extra: Any) -> Generator["ContextLogger", None, None]:
        yield ContextLogger(**{**self.extra, **extra})

    def bind(self, **extra: Any) -> "ContextLogger":
        return ContextLogger(**{**self.extra, **extra})

    def _elapsed(self) -> float:
        return round(time.perf_counter() - self._start, 4)

    def started(self, **kwargs: Any) -> None:
        self._logger.bind(status="STARTED", **kwargs).info("event.started")

    def success(self, **kwargs: Any) -> None:
        self._logger.bind(status="SUCCESS", elapsed=self._elapsed(), **kwargs).info("event.success")

    def failed(self, exc: Optional[BaseException] = None, **kwargs: Any) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else None
        self._logger.bind(status="FAILED", elapsed=self._elapsed(), traceback=tb, **kwargs).error(
            "event.failed"
        )

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.bind(**kwargs).debug(msg)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.bind(**kwargs).info(msg)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.bind(**kwargs).warning(msg)

    def error(self, msg: str, exc: Optional[BaseException] = None, **kwargs: Any) -> None:
        self._logger.bind(**kwargs).opt(exception=exc).error(msg)

    def exception(self, msg: str, **kwargs: Any) -> None:
        self._logger.bind(**kwargs).exception(msg)


_default_logger = ContextLogger()
context_logger: ContextVar[ContextLogger] = ContextVar("context_logger", default=_default_logger)


def get_logger() -> ContextLogger:
    return context_logger.get(_default_logger)


def log_execution(_fn=None, *, graceful_failure: bool = False):
    """Decorator: logs start/success/failure of a sync or async callable."""

    def decorator(fn):
        is_async = inspect.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            log = get_logger().bind(func=fn.__name__)
            log.started()
            try:
                result = await fn(*args, **kwargs)
                log.success()
                return result
            except Exception as exc:
                log.failed(exc=exc)
                if graceful_failure:
                    return None
                raise

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            log = get_logger().bind(func=fn.__name__)
            log.started()
            try:
                result = fn(*args, **kwargs)
                log.success()
                return result
            except Exception as exc:
                log.failed(exc=exc)
                if graceful_failure:
                    return None
                raise

        return async_wrapper if is_async else sync_wrapper

    if _fn is None:
        return decorator
    return decorator(_fn)
