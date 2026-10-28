"""CDC rung: change capture from postgres logical decoding, polled.

The honest shape of this rung. The server decodes its WAL through the
built-in ``test_decoding`` output plugin. I create a logical replication
slot, let writes happen, then poll ``pg_logical_slot_get_changes`` over a
plain SQL connection and parse the text lines into bronze rows. Each row
carries ``_op`` (insert, update, delete), ``_table`` (as the plugin
prints it, schema qualified), ``_lsn``, and the changed columns as
strings. ``get_changes`` consumes: a second capture returns only what
happened since the first. That is slot semantics, nothing more.

Not claimed: Debezium itself, any non-postgres source, streaming protocol
consumption, or delivery guarantees beyond what a replication slot gives.
A line the parser does not understand raises with that line in the
message. Nothing is silently dropped.

Credentials: callers pass a connection uri. The connector spec never
holds one; it names an environment variable (see ``connectors.py``).
"""
from __future__ import annotations

from typing import Any

import pyarrow as pa

_PLUGIN = "test_decoding"
_OPS = {"INSERT": "insert", "UPDATE": "update", "DELETE": "delete"}


def create_slot(uri: str, slot: str) -> None:
    """Create a logical replication slot using test_decoding. Changes made
    after this call are captured; changes before it are gone, by design."""
    import psycopg

    with psycopg.connect(uri, autocommit=True) as con:
        con.execute(
            "select pg_create_logical_replication_slot(%s, %s)", (slot, _PLUGIN))


def drop_slot(uri: str, slot: str) -> None:
    """Drop the slot. The server releases the retained WAL. A missing slot
    raises; dropping is an explicit act and a typo should be loud."""
    import psycopg

    with psycopg.connect(uri, autocommit=True) as con:
        con.execute("select pg_drop_replication_slot(%s)", (slot,))


def capture(uri: str, slot: str) -> pa.Table:
    """Poll the slot and return the changes since the last capture as an
    Arrow table of strings. Consuming: the slot advances, so the same
    change never comes back twice. A missing slot raises from the server;
    I never swallow that.
    """
    import psycopg

    from .legacy import rows_to_table

    rows: list[dict[str, Any]] = []
    with psycopg.connect(uri, autocommit=True) as con:
        cur = con.execute(
            "select lsn::text, xid, data from pg_logical_slot_get_changes(%s, null, null)",
            (slot,))
        for lsn, _xid, data in cur:
            if data.startswith("BEGIN ") or data.startswith("COMMIT "):
                continue
            rows.append(parse_change(lsn, data))
    return rows_to_table(rows)


# ------------------------------------------------------------------ parsing

def parse_change(lsn: str, line: str) -> dict[str, Any]:
    """Parse one test_decoding change line into a bronze row.

    The plugin prints ``table <schema>.<name>: <OP>: <columns>``. Column
    text is ``name[type]:value`` pairs: names are quoted with doubled
    double quotes when they need it, values are quoted with doubled
    single quotes when they are not plain numerals, and null is the bare
    word null. An UPDATE that changed its key prints ``old-key: ...
    new-tuple: ...``; the new tuple is the row that exists now, so that
    is what lands. A DELETE on a table with no replica identity prints
    ``(no-tuple-data)`` and lands with no columns beyond the metadata.
    Anything that does not fit raises with the offending line.
    """
    head, sep, rest = line.partition(": ")
    if not sep or not head.startswith("table "):
        raise ValueError(f"cdc: cannot parse change line: {line!r}")
    table = head[len("table "):]
    op_word, _, cols_text = rest.partition(": ")
    if op_word not in _OPS:
        raise ValueError(f"cdc: unknown operation {op_word!r} in line: {line!r}")

    if cols_text.startswith("old-key:"):
        _, sep, cols_text = cols_text.partition(" new-tuple: ")
        if not sep:
            raise ValueError(f"cdc: old-key without new-tuple in line: {line!r}")
    row: dict[str, Any] = {"_op": _OPS[op_word], "_table": table, "_lsn": lsn}
    if cols_text.strip() == "(no-tuple-data)":
        return row
    row.update(_parse_columns(cols_text, line))
    return row


def _parse_columns(text: str, line: str) -> dict[str, Any]:
    cols: dict[str, Any] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] == " ":
            i += 1
        if i >= n:
            break
        if text[i] == '"':
            name, i = _scan_quoted(text, i, '"', line)
        else:
            j = text.find("[", i)
            if j <= i:
                raise ValueError(f"cdc: cannot parse column name in line: {line!r}")
            name, i = text[i:j], j
        if i >= n or text[i] != "[":
            raise ValueError(f"cdc: missing type annotation in line: {line!r}")
        j = text.find("]:", i)  # type names like integer[] still end at "]:"
        if j == -1:
            raise ValueError(f"cdc: unterminated type annotation in line: {line!r}")
        i = j + 2
        if i < n and text[i] == "'":
            value, i = _scan_quoted(text, i, "'", line)
        else:
            j = text.find(" ", i)
            if j == -1:
                j = n
            token = text[i:j]
            if not token:
                raise ValueError(f"cdc: missing value in line: {line!r}")
            value = None if token == "null" else token
            i = j
        cols[name] = value
    if not cols:
        raise ValueError(f"cdc: change line has no columns: {line!r}")
    return cols


def _scan_quoted(text: str, i: int, quote: str, line: str) -> tuple[str, int]:
    """Scan a quote-delimited token starting at ``i``. A doubled quote is
    an escaped literal quote, the test_decoding convention for both
    identifiers and values."""
    i += 1  # past the opening quote
    out: list[str] = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == quote:
            if i + 1 < n and text[i + 1] == quote:
                out.append(quote)
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ValueError(f"cdc: unterminated quoted token in line: {line!r}")
