"""JSON lines on stdout, one object per line, for whatever ships a container's logs.

The rule is that a line says what happened and to whom, never with what. So emit()
takes ids, names and numbers, and an exception is written as its type. A database
or parser message can quote a tenant's cell, and the worst place to find that out
is a log aggregator. The formatter holds the same line for every other ironwright
logger: an exception passed as an argument comes out as its type.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any

_root = logging.getLogger("ironwright")
_log = logging.getLogger("ironwright.obs")
_lock = threading.Lock()
_handler: logging.Handler | None = None


def _safe(v: Any) -> Any:
    return type(v).__name__ if isinstance(v, BaseException) else v


def _message(record: logging.LogRecord) -> str:
    """record.getMessage() with exceptions swapped for their type. Named arguments
    are not ours to read, so those lines keep the template and nothing else."""
    msg = str(_safe(record.msg))
    if not isinstance(record.args, tuple) or not record.args:
        return msg
    try:
        return msg % tuple(_safe(a) for a in record.args)
    except (TypeError, ValueError):
        return msg


class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname.lower(), "logger": record.name, "event": _message(record)}
        if record.exc_info and record.exc_info[0]:
            line["error_type"] = record.exc_info[0].__name__
        line.update(getattr(record, "obs", {}))
        return json.dumps(line, default=str)


class _Stdout(logging.Handler):
    """Looks sys.stdout up on every line, so it follows whatever owns it now."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001  a closed pipe must not take a request down with it
            self.handleError(record)


def configure() -> None:
    """Idempotent. Propagation stays on, so anything above can still listen."""
    global _handler
    with _lock:
        if _handler is None:
            _handler = _Stdout()
            _handler.setFormatter(JsonLines())
            _root.addHandler(_handler)
            if _root.level == logging.NOTSET:
                _root.setLevel(logging.INFO)


def emit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    configure()
    _log.log(level, event, extra={"obs": fields})
