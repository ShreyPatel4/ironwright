"""One guard and one runner for user-written SQL (the Explorer, board tiles).

The guard is discipline; the defense is the connection, which disables
external access and locks its configuration (lake.connect). The caps here
bound what a query that gets past both can cost. Clients get a short category
and an error id; the full DuckDB text only ever goes to the server log.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid

import duckdb

MAX_ROWS = 500
MEMORY_LIMIT = "1GB"
THREADS = 2
TIMEOUT_SECONDS = 10
# At most this many SQL requests running for one org, per API process. Each can
# hold MEMORY_LIMIT and THREADS, so this is what one tenant can cost the others.
MAX_CONCURRENT_PER_ORG = 3
# One run walks every tile in turn, each with its own TIMEOUT_SECONDS.
MAX_BOARD_TILES = 24

_log = logging.getLogger("ironwright.sql")
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# DuckDB normalizes some of these to a real semicolon, so none get a pass.
_SEMICOLONS = frozenset(";;؛︔﹔；")


_slots: dict[str, int] = {}
_slots_lock = threading.Lock()


class SqlRejected(ValueError):
    """The statement never reached a connection. The message is ours, so it
    is safe to show."""


class SqlBusy(Exception):
    """The org already has MAX_CONCURRENT_PER_ORG SQL requests running."""


def take_slot(org_id: str) -> None:
    """Never waits: a queue would hold a server thread for every request over the cap."""
    with _slots_lock:
        if _slots.get(org_id, 0) >= MAX_CONCURRENT_PER_ORG:
            raise SqlBusy(org_id)
        _slots[org_id] = _slots.get(org_id, 0) + 1


def give_slot(org_id: str) -> None:
    with _slots_lock:
        left = _slots.get(org_id, 0) - 1
        if left > 0:
            _slots[org_id] = left
        else:
            _slots.pop(org_id, None)


def slots_held() -> dict[str, int]:
    with _slots_lock:
        return dict(_slots)


def guard_select(sql: str) -> str:
    """One statement, SELECT or WITH, nothing after a terminating semicolon.
    The leading keyword rules out PRAGMA and SHOW, which parse as SELECT. The
    real parser rules out WITH ... INSERT, which starts like a read."""
    body = sql.strip().rstrip(";").strip()
    if "\x00" in body:
        raise SqlRejected("NUL bytes are not allowed in a query")
    if not _SEMICOLONS.isdisjoint(body):
        raise SqlRejected("one statement at a time")
    if not _SELECT_ONLY.match(body):
        raise SqlRejected("read-only: the statement must start with SELECT or WITH")
    # A throwaway connection: the module default is shared across request threads.
    parser = duckdb.connect()
    try:
        statements = parser.extract_statements(body)
    except duckdb.Error as e:
        raise SqlRejected("could not parse that SQL") from e
    finally:
        parser.close()
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise SqlRejected("read-only: the statement must be a single SELECT")
    return body


def apply_limits(con: duckdb.DuckDBPyConnection) -> None:
    """Must run before the configuration is locked. Spill stays off so a big
    sort cannot fill the disk. The directories are blank because duckdb_settings()
    is readable by any SELECT, and a default holds the server's home path."""
    con.execute(f"set memory_limit = '{MEMORY_LIMIT}'")
    con.execute(f"set threads = {THREADS}")
    for directory in ("temp_directory", "home_directory", "extension_directory", "secret_directory"):
        con.execute(f"set {directory} = ''")


def run_capped(con: duckdb.DuckDBPyConnection, sql: str,
               cap: int = MAX_ROWS) -> tuple[list[str], list[list[str | None]]]:
    """Execute and fetch under one wall clock. Results stream, so the work can
    happen on fetch; the timer has to cover both. Joining it before returning
    means a late interrupt cannot land on the caller's next query."""
    timer = threading.Timer(TIMEOUT_SECONDS, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(min(cap, MAX_ROWS))
    finally:
        timer.cancel()
        timer.join()
    return cols, [[None if v is None else str(v) for v in r] for r in rows]


def _category(e: Exception) -> str:
    if isinstance(e, duckdb.PermissionException):
        return "file and network access is disabled here"
    if isinstance(e, duckdb.InterruptException):
        return f"the query hit the {TIMEOUT_SECONDS} second time limit"
    if isinstance(e, duckdb.OutOfMemoryException):
        return "the query hit the memory limit"
    if isinstance(e, duckdb.ParserException):
        return "could not parse that SQL"
    if isinstance(e, (duckdb.BinderException, duckdb.CatalogException)):
        return "unknown table, column or function"
    return "the query failed"


def safe_error(e: Exception) -> str:
    """What the client may see. Logs the full text under the id it returns."""
    if isinstance(e, SqlRejected):
        return str(e)
    error_id = uuid.uuid4().hex[:8]
    _log.error("sql error %s: %s", error_id, e)
    return f"{_category(e)} (error {error_id})"
