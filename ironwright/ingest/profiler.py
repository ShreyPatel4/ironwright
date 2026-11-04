"""What is in an uploaded file: layout, types, nulls, keys and joins.

Deterministic and rule based. No LLM, no randomness, no clock, no set order in
the output, so a golden file can pin it. Every guess carries a confidence and a
reason, and reasons carry counts and rule names only, never cell text: samples
are the one place tenant values appear, and they are capped. Samples are never
sent to an LLM unless fenced (`llm.untrusted`).

Limits, on purpose: EBCDIC is refused at the door, candidate keys are single
columns, and two abutting text fields with no gutter read as one field. The user
approves the layout, so a merged field is visible and cheap to fix.
"""
from __future__ import annotations

import codecs
import csv
import difflib
import io
import json
import re
import zlib
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import yaml

from ..config import FILE_NAME, safe_child, write_private
from . import uploads
from .propose import propose

MAX_ROWS = 200_000
MAX_BYTES = 64 * 1024 ** 2
MAX_FILES = 200
_SAMPLES = 5
_SAMPLE_CHARS = 64
_JOIN_VALUES = 5_000
_SNIFF_LINES = 200
_MAX_TRAILER = 3
_MAX_BAD_LINES = 20
_NAME_CHARS = 40
_BOUNDARY_SAMPLE = 20_000
_LINE_END = re.compile(r"\r\n|\n|\r")

_DELIMITERS = (",", "\t", "|", ";")
_MIN_DELIMITER_SHARE = 0.8
_MIN_FIXED_SHARE = 0.8
_ZERO_PAD_RUN = 3
_MIN_JOIN_OVERLAP = 0.5
_MIN_NAME_SCORE = 0.8

NULL_TOKENS = {"", "null", "n/a", "0000-00-00"}
_BOOL_PAIRS = ({"y", "n"}, {"yes", "no"}, {"t", "f"}, {"true", "false"})
_INT = re.compile(r"[+-]?\d+")
_DECIMAL = re.compile(r"[+-]?(\d*)\.(\d+)")
_INT64_DIGITS = 19
_INT64_MAX = 2 ** 63 - 1
_MAX_PRECISION = 38
_DATE_NAME = re.compile(r"(^|_)(date|dt|day)(_|$)")
_KEY_NAME = re.compile(r"(^|_)(id|key|nbr|no|num|number|code)$")
_KEY_SUFFIX = re.compile(r"_(id|key|nbr|no|num|number|code)$")
_TRAILER = re.compile(r"\s*([*=#-]{2,}|total\b|grand total\b|end of\b|eof\b|trailer\b|trl\b|record count\b)", re.IGNORECASE)
_RECORD_TRAILER = re.compile(r"(TRL|TRAILER|EOF|END)([^A-Za-z]|$)")
_RECORD_HEADER = re.compile(r"(HDR|HEADER)([^A-Za-z]|$)")
_GENERATED_NAME = re.compile(r"(field|column)_\d+")
_TRAILING_NUMBER = re.compile(r"_?\d+$")
_HEADER_CELL = re.compile(r"[A-Za-z_][\w .#/()-]*")

# Earlier wins a tie. Month first over day first is a choice, and the column says so.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d/%m/%y", "%m-%d-%Y",
                 "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y")
_COMPACT_DATE = "%Y%m%d"
_ORDER_PAIRS = {"%m/%d/%Y": "%d/%m/%Y", "%m/%d/%y": "%d/%m/%y", "%m-%d-%Y": "%d-%m-%Y"}
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M",
                      "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")
_RESERVED = {"select", "from", "where", "group", "order", "by", "table", "join", "on", "and", "or", "not",
             "null", "case", "when", "then", "else", "end", "as", "in", "is", "limit", "union", "all",
             "distinct", "default", "primary", "check", "user", "month", "year", "day", "hour", "minute",
             "second", "date", "time", "timestamp"}


# ----------------------------------------------------------------- names


def snake(raw: str, fallback: str) -> str:
    """A safe identifier from tenant text. Only [a-z0-9_] survives, so a header
    can never carry markup, a citation or a link into anything built from it."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:_NAME_CHARS].strip("_")
    if not s:
        return fallback
    if s[0].isdigit():
        s = f"c_{s}"[:_NAME_CHARS]
    return f"{s}_" if s in _RESERVED else s


def unique_names(names: Iterable[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for n in names:
        seen[n] += 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


# --------------------------------------------------------------- decoding


def _decode(raw: bytes, cut: bool) -> tuple[str, str, float, str]:
    """(text, codec, confidence, reason). A file cut at a byte cap may end mid
    character, so the decoder is told not to expect a clean end."""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                     (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return (codecs.getincrementaldecoder(enc)().decode(raw, final=not cut), enc, 1.0,
                    f"The file starts with a {enc} byte order mark.")
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not cut)
    except UnicodeDecodeError:
        pass
    else:
        if raw.isascii():
            return text, "utf-8", 0.9, "Every byte is ASCII, which UTF-8 reads the same way."
        return text, "utf-8", 0.99, "The non-ASCII bytes form valid UTF-8."
    try:
        return raw.decode("cp1252"), "cp1252", 0.6, "Not valid UTF-8. Every byte is defined in cp1252, a common Windows export."
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1", 0.4, "Not valid UTF-8 or cp1252. latin-1 reads anything, so check the accents."


# ----------------------------------------------------------- type inference


def _parse(value: str, fmt: str) -> bool:
    try:
        return 1900 <= datetime.strptime(value, fmt).year <= 2100  # noqa: DTZ007 - shape check, no time zone in play
    except ValueError:
        return False


def _cover(counts: Counter[str], formats: Iterable[str]) -> dict[str, int] | None:
    """Fewest formats that explain every value, greedy by rows covered, list
    order breaking a tie. None when some value fits no format."""
    fits = {f: {v for v in counts if _parse(v, f)} for f in formats}
    left = set(counts)
    out: dict[str, int] = {}
    while left:
        best = max(fits, key=lambda f: sum(counts[v] for v in fits[f] & left))
        got = fits[best] & left
        if not got:
            return None
        out[best] = sum(counts[v] for v in got)
        left -= got
    return out


def _temporal(counts: Counter[str], name: str) -> dict[str, Any] | None:
    # One unparseable value among the first few settles it without trying the rest.
    head = Counter(dict(list(counts.items())[:20]))
    date_formats = list(_DATE_FORMATS)
    if _DATE_NAME.search(name):
        date_formats.append(_COMPACT_DATE)
    for kind, formats in (("date", date_formats), ("timestamp", _TIMESTAMP_FORMATS)):
        if _cover(head, formats) is None:
            continue
        found = _cover(counts, formats)
        if found is None:
            continue
        total = sum(found.values())
        fmt = max(found, key=lambda f: (found[f], -list(formats).index(f)))
        out: dict[str, Any] = {"type": kind, "format": fmt, "formats": found}
        alt = _ORDER_PAIRS.get(fmt)
        if len(found) > 1:
            out["confidence"] = round(found[fmt] / total, 4)
            out["reason"] = f"Every value parses, but under {len(found)} formats. The dominant one holds this share of the rows."
        elif alt and all(_parse(v, alt) for v in counts):
            out["ambiguous_with"] = alt
            out["confidence"] = 0.5
            out["reason"] = "Every value fits month first and day first. I picked month first. Check this one."
        else:
            out["confidence"] = 0.95
            out["reason"] = f"Every value parses as a {kind} in one format."
        return out
    return None


def _numeric(values: list[str]) -> dict[str, Any] | None:
    int_digits = scale = 0
    is_decimal = False
    for v in values:
        body = v.lstrip("+-")
        m = _DECIMAL.fullmatch(v)
        if m:
            is_decimal = True
            whole, frac = m.group(1), m.group(2)
        elif _INT.fullmatch(v):
            whole, frac = body, ""
        else:
            return None
        if len(whole) > 1 and whole[0] == "0":
            return {"type": "string", "confidence": 0.9,
                    "reason": "Numbers with leading zeros stay strings. The zeros may mean something, as in an id or a UPC. Cast it later if it is a quantity."}
        int_digits = max(int_digits, 0 if whole == "0" else len(whole))
        scale = max(scale, len(frac))
    if is_decimal:
        if int_digits + scale > _MAX_PRECISION:
            return None
        return {"type": f"decimal({int_digits + scale},{scale})", "confidence": 0.95,
                "reason": "Every value is a plain number with a decimal point in some rows. Precision and scale are the widest seen."}
    # The length check comes first: int() raises past 4300 digits.
    if int_digits > _INT64_DIGITS or any(abs(int(v)) > _INT64_MAX for v in values):
        return None
    return {"type": "int", "confidence": 0.95, "reason": "Every value is a whole number with no leading zeros."}


def _infer(counts: Counter[str], name: str) -> dict[str, Any]:
    values = list(counts)
    if not values:
        return {"type": "string", "confidence": 0.2, "reason": "Every value is null, so there is nothing to infer from."}
    lowered = {v.lower() for v in values}
    if lowered in _BOOL_PAIRS:
        return {"type": "bool", "confidence": 0.9, "reason": "Exactly one true and false spelling, both seen. 0 and 1 stay int, I do not guess a flag."}
    # Eight digits read as a date only under a date-like name, so this runs first.
    temporal = _temporal(counts, name)
    if temporal:
        return temporal
    return _numeric(values) or {"type": "string", "confidence": 0.8, "reason": "Some values fit no narrower type, so string keeps every byte."}


def _normal(value: str) -> str:
    """001001 and 1001 are the same id when one system pads and the other does not."""
    return (value.lstrip("0") or "0") if value.isdigit() else value


def _bucket(value: str) -> int:
    return zlib.crc32(value.encode("utf-8", "surrogatepass"))


def _join_sample(values: Iterable[str]) -> tuple[int, set[str]]:
    """(k, the values whose hash has k low zero bits). A cap by hash, not by first
    seen: two files in different row orders still hold the same slice of the ids,
    and two columns compare fairly once both are cut to the larger k."""
    kept, k = {_normal(v) for v in values}, 0
    while len(kept) > _JOIN_VALUES:
        k += 1
        kept = {v for v in kept if _bucket(v) % (1 << k) == 0}
    return k, kept


def _profile_column(name: str, source: str, cells: list[str]) -> tuple[dict[str, Any], tuple[int, set[str]]]:
    null_tokens: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for raw in cells:
        v = raw.strip()
        if v.lower() in NULL_TOKENS:
            null_tokens[v] += 1
        else:
            counts[v] += 1
    rows, nulls, present = len(cells), sum(null_tokens.values()), sum(counts.values())
    out = {"name": name, "source_name": source[:_SAMPLE_CHARS], "nulls": nulls,
           "null_rate": round(nulls / rows, 4) if rows else 0.0,
           "null_tokens": dict(sorted(null_tokens.items())), "distinct": len(counts),
           "distinct_ratio": round(len(counts) / present, 4) if present else 0.0,
           "samples": [v[:_SAMPLE_CHARS] for v in list(counts)[:_SAMPLES]],
           **_infer(counts, name)}
    return out, _join_sample(counts)


# ----------------------------------------------------------------- headers


def _is_header(first: list[str], rest: list[list[str]]) -> tuple[bool, float, str]:
    """Is the first row names rather than data? Strong when a typed column sits
    under a cell that does not fit the type. Weak when everything is text."""
    if not rest:
        return False, 0.5, "There is one row, so I read it as data."
    cells = [c.strip() for c in first]
    if any(_numeric([c]) for c in cells if c):
        return False, 0.9, "The first row holds a number, so it is data."
    if any(_temporal(Counter([c]), "date") for c in cells if c):
        return False, 0.9, "The first row holds a date, so it is data."
    for i, c in enumerate(cells):
        below = Counter(r[i].strip() for r in rest if i < len(r) and r[i].strip().lower() not in NULL_TOKENS)
        if c and below and _infer(below, "")["type"] != "string":
            return True, 0.95, "The first row is text above a column of typed values."
    if all(_HEADER_CELL.fullmatch(c) for c in cells) and len({c.lower() for c in cells}) == len(cells):
        return True, 0.6, "Every column is text, so this is weaker: the first row looks like distinct names."
    return False, 0.6, "The first row does not look like distinct names."


def _column_names(first: list[str], header: bool, width: int) -> tuple[list[str], list[str]]:
    if not header:
        names = [f"field_{i + 1}" for i in range(width)]
        return names, names
    sources = [(first[i] if i < len(first) else "").strip() for i in range(width)]
    return unique_names(snake(s, f"column_{i + 1}") for i, s in enumerate(sources)), sources


# -------------------------------------------------------------- delimiters


def _read(text: str, delimiter: str, limit: int | None = None) -> list[tuple[int, list[str]]]:
    """(line number, cells) for each non-blank record. csv.Error propagates."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    out = []
    for cells in reader:
        if cells and any(c.strip() for c in cells):
            out.append((reader.line_num, cells))
            if limit is not None and len(out) >= limit:
                break
    return out


def _sniff_delimiter(text: str) -> tuple[str, float] | None:
    best: tuple[float, int, int, str] | None = None
    for rank, d in enumerate(_DELIMITERS):
        widths = Counter(len(cells) for _, cells in _read(text, d, _SNIFF_LINES))
        if not widths:
            continue
        width, hits = max(widths.items(), key=lambda kv: (kv[1], kv[0]))
        share = hits / sum(widths.values())
        if width >= 2 and share >= _MIN_DELIMITER_SHARE:
            candidate = (share, width, -rank, d)
            if best is None or candidate > best:
                best = candidate
    return (best[3], best[0]) if best else None


def _profile_delimited(name: str, text: str, delimiter: str, cut: bool) -> dict[str, Any] | None:
    rows = _read(text, delimiter)
    if not rows:
        return None
    width = Counter(len(c) for _, c in rows).most_common(1)[0][0]
    header, conf, reason = _is_header(rows[0][1], [c for _, c in rows[1:_SNIFF_LINES] if len(c) == width])
    first = rows[0][1]
    body = rows[1:] if header else rows
    trailer = 0
    if not cut:
        while body and trailer < _MAX_TRAILER and (len(body[-1][1]) != width or _TRAILER.match(body[-1][1][0])):
            body = body[:-1]
            trailer += 1
    bad = [line for line, c in body if len(c) != width]
    good = [c for _, c in body if len(c) == width]
    truncated = cut or len(good) > MAX_ROWS
    good = good[:MAX_ROWS]
    names, sources = _column_names(first, header, width)
    return {"format": "csv", "layout": {"delimiter": delimiter, "header": header}, "header_confidence": conf,
            "header_reason": reason, "rows": len(good), "truncated": truncated, "leader_rows": 0, "trailer_rows": trailer,
            "bad_rows": {"count": len(bad), "lines": bad[:_MAX_BAD_LINES]},
            "_cells": good, "_names": names, "_sources": sources}


# ------------------------------------------------------------- fixed width


def _position_stats(lines: list[str], length: int) -> list[dict[str, Any]]:
    stats = []
    for i in range(length):
        chars = {ln[i] for ln in lines}
        stats.append({"chars": chars, "blank": chars == {" "}, "digit": all(c.isdigit() for c in chars),
                      "no_digit": not any(c.isdigit() for c in chars), "letter": any(c.isalpha() for c in chars)})
    return stats


def _boundaries(lines: list[str], length: int) -> list[dict[str, Any]]:
    """Field starts from column alignment. Three signals, weakest last:
    a gutter blank in every row; a change between all digits and no digits;
    a varied digit followed by a run of constant zeros, which is a zero padded
    field abutting the one before it."""
    st = _position_stats(lines[:_BOUNDARY_SAMPLE], length)
    starts = [{"start": 0, "boundary": "record_start", "confidence": 1.0}]
    for i in range(1, length):
        prev, cur = st[i - 1], st[i]
        if cur["blank"]:
            continue
        if prev["blank"]:
            if all(p["blank"] for p in st[:i]):
                continue  # padding in front of a right justified first field
            lone = not st[i - 2]["blank"] and st[i - 2]["letter"] and cur["letter"]
            if not st[i - 2]["blank"] and _constant_prefix(st, i - 1, starts[-1]["start"]):
                continue  # one blank after text that never changes: "Brightline #101"
            starts.append({"start": i, "boundary": "gutter", "confidence": 0.6 if lone else 0.9})
        elif (
            (prev["digit"] and cur["no_digit"] and cur["letter"])
            or (prev["no_digit"] and prev["letter"] and cur["digit"]
                and not _constant_prefix(st, i, starts[-1]["start"]))
        ):
            starts.append({"start": i, "boundary": "class_change", "confidence": 0.8})
        elif prev["digit"] and len(prev["chars"]) > 1 and _zero_run(st, i) >= _ZERO_PAD_RUN:
            starts.append({"start": i, "boundary": "zero_pad", "confidence": 0.5})
    for here, after in zip(starts, [*starts[1:], {"start": length}]):
        here["width"] = after["start"] - here["start"]
    return starts


def _constant_prefix(st: list[dict[str, Any]], i: int, field_start: int) -> bool:
    """SKU00123: letters that never change are part of the code, not a field."""
    return all(len(st[p]["chars"]) == 1 for p in range(field_start, i))


def _zero_run(st: list[dict[str, Any]], i: int) -> int:
    run = 0
    while i + run < len(st) and st[i + run]["chars"] == {"0"}:
        run += 1
    return run


def _sniff_fixed(lines: list[str]) -> int | None:
    """The record length, when most lines share one and they are long enough to split."""
    if len(lines) < 2:
        return None
    length, hits = Counter(len(ln) for ln in lines).most_common(1)[0]
    if length < 2 or hits / len(lines) < _MIN_FIXED_SHARE or hits < 2:
        return None
    records = [ln for ln in lines if len(ln) == length]
    return length if len(_boundaries(records, length)) >= 2 else None


def _profile_fixed(name: str, lines: list[str], length: int, cut: bool) -> dict[str, Any]:
    leader = 0
    while leader < len(lines) and (len(lines[leader]) != length or _RECORD_HEADER.match(lines[leader])):
        if leader >= _MAX_TRAILER:
            break
        leader += 1
    body = list(enumerate(lines, 1))[leader:]
    trailer = 0
    if not cut:
        while body and trailer < _MAX_TRAILER and (len(body[-1][1]) != length or _RECORD_TRAILER.match(body[-1][1])):
            body = body[:-1]
            trailer += 1
    bad = [n for n, ln in body if len(ln) != length]
    records = [ln for _, ln in body if len(ln) == length]

    header, conf, reason = False, 0.7, "Mainframe records usually carry no names, so fields are numbered."
    fields = _boundaries(records, length)
    if len(records) > 2:
        below = _boundaries(records[1:], length)
        first = [records[0][f["start"]:f["start"] + f["width"]] for f in below]
        rest = [[ln[f["start"]:f["start"] + f["width"]] for f in below] for ln in records[1:_SNIFF_LINES]]
        is_header, h_conf, h_reason = _is_header(first, rest)
        if is_header and len(below) >= 2:
            header, conf, reason, fields = True, h_conf, h_reason, below
    head_cells = [records[0][f["start"]:f["start"] + f["width"]] for f in fields] if records else []
    data = records[1:] if header else records
    truncated = cut or len(data) > MAX_ROWS
    data = data[:MAX_ROWS]
    names, sources = _column_names(head_cells, header, len(fields))
    for f, n in zip(fields, names):
        f["name"] = n
    cells = [[ln[f["start"]:f["start"] + f["width"]] for f in fields] for ln in data]
    return {"format": "fixed_width", "layout": {"fields": fields, "header": header}, "header_confidence": conf,
            "header_reason": reason, "rows": len(data), "truncated": truncated, "leader_rows": leader,
            "trailer_rows": trailer, "bad_rows": {"count": len(bad), "lines": bad[:_MAX_BAD_LINES]},
            "_cells": cells, "_names": names, "_sources": sources}


# ------------------------------------------------------------------- files


def _candidate_keys(columns: list[dict[str, Any]], rows: int, truncated: bool) -> list[dict[str, Any]]:
    found = []
    for c in columns:
        if rows < 2 or c["nulls"] or c["distinct"] != rows or c["type"] not in ("int", "string"):
            continue
        named = bool(_KEY_NAME.search(c["name"]))
        conf = 0.9 if named else 0.6
        reason = "Unique and never null in every row read."
        if named:
            reason += " The name also reads like a key."
        if truncated:
            conf = 0.5 if named else 0.4
            reason += " The file was cut at a cap, so the rows I did not read could break it."
        found.append({"column": c["name"], "confidence": conf, "reason": reason})
    return sorted(found, key=lambda k: -k["confidence"])


def _profile_file(name: str, raw: bytes) -> tuple[dict[str, Any], dict[str, tuple[int, set[str]]]]:
    cut = len(raw) > MAX_BYTES
    text, encoding, enc_conf, enc_reason = _decode(raw[:MAX_BYTES], cut)
    if cut:  # the last line is whole only by luck
        text = text[:text.rfind("\n") + 1]
    # Not str.splitlines: that also breaks on form feeds and other characters
    # a mainframe record can legally hold.
    lines = [ln for ln in _LINE_END.split(text) if ln.strip()]
    sniffed = _sniff_delimiter(text)
    if sniffed:
        d, share = sniffed
        out = _profile_delimited(name, text, d, cut)
        fmt_conf = round(min(0.95, share), 4)
        fmt_reason = "One delimiter gives the same field count on most rows."
    else:
        length = _sniff_fixed(lines)
        if length:
            out = _profile_fixed(name, lines, length, cut)
            fmt_conf = 0.8
            fmt_reason = "No delimiter fits, most lines share one length, and the columns line up."
        else:
            out = _profile_delimited(name, text, ",", cut)
            fmt_conf = 0.3
            fmt_reason = "No delimiter fits and the lines do not align, so I read one text column. Check this file."
    if out is None:
        return {"name": name, "error": "The file holds no rows."}, {}
    cells, names, sources = out.pop("_cells"), out.pop("_names"), out.pop("_sources")
    columns, values = [], {}
    for i, (n, s) in enumerate(zip(names, sources)):
        col, vals = _profile_column(n, s, [r[i] for r in cells])
        columns.append(col)
        values[n] = vals
    out["layout"]["encoding"] = encoding
    return ({"name": name, "source": snake(name.rsplit(".", 1)[0], "source"), "format_confidence": fmt_conf, "format_reason": fmt_reason, "encoding_confidence": enc_conf,
             "encoding_reason": enc_reason, **out, "columns": columns,
             "candidate_keys": _candidate_keys(columns, out["rows"], out["truncated"])}, values)


# ------------------------------------------------------------------- joins


def _join_stem(column: str, file_name: str) -> str:
    if column == "id":
        stem = snake(file_name.rsplit(".", 1)[0], "")
        return re.sub(r"ies$", "y", stem) if stem.endswith("ies") else stem.removesuffix("s")
    return _KEY_SUFFIX.sub("", column)


def _name_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if _GENERATED_NAME.fullmatch(a) or _GENERATED_NAME.fullmatch(b):
        return 0.0  # I made these names up, so they are no evidence of anything
    if _TRAILING_NUMBER.sub("", a) == _TRAILING_NUMBER.sub("", b):
        return 0.0  # addr_1 and addr_2 are siblings, not the same thing
    short, long = sorted((a, b), key=len)
    if len(short) >= 3 and long.startswith(short):
        return 0.85
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def _candidate_joins(files: list[dict[str, Any]], values: dict[str, dict[str, tuple[int, set[str]]]]) -> list[dict[str, Any]]:
    joins = []
    usable = [f for f in files if "error" not in f]
    for i, left in enumerate(usable):
        for right in usable[i + 1:]:
            for a in left["columns"]:
                for b in right["columns"]:
                    join = _join(left, a, values[left["name"]][a["name"]], right, b, values[right["name"]][b["name"]])
                    if join:
                        joins.append(join)
    return sorted(joins, key=lambda j: (-j["confidence"], j["left"], j["right"]))


def _join(lf: dict[str, Any], a: dict[str, Any], a_vals: tuple[int, set[str]],
          rf: dict[str, Any], b: dict[str, Any], b_vals: tuple[int, set[str]]) -> dict[str, Any] | None:
    if a["type"] not in ("int", "string") or b["type"] not in ("int", "string"):
        return None
    a_key = a["distinct"] == lf["rows"] and not a["nulls"]
    b_key = b["distinct"] == rf["rows"] and not b["nulls"]
    if not (a_key or b_key):
        return None  # a shared low cardinality attribute, not a relationship
    score = _name_score(_join_stem(a["name"], lf["name"]), _join_stem(b["name"], rf["name"]))
    if score < _MIN_NAME_SCORE:
        return None
    k = max(a_vals[0], b_vals[0])
    a_set = {v for v in a_vals[1] if _bucket(v) % (1 << k) == 0}
    b_set = {v for v in b_vals[1] if _bucket(v) % (1 << k) == 0}
    if not a_set or not b_set:
        return None
    shared = len(a_set & b_set)
    overlap = round(shared / min(len(a_set), len(b_set)), 4)
    if overlap < _MIN_JOIN_OVERLAP:
        return None
    left, right = f"{lf['name']}.{a['name']}", f"{rf['name']}.{b['name']}"
    parent, child = (a_set, b_set) if a_key else (b_set, a_set)
    orphans = len(child - parent)
    return {"left": left, "right": right, "unique_side": left if a_key else right, "name_score": score,
            "overlap": overlap, "orphans": orphans, "sampled": k > 0, "confidence": round(min(score, overlap), 4),
            "reason": (f"The names are similar and {shared} distinct values match after dropping leading zeros. "
                       f"Values on the many side with no match: {orphans}."
                       + (" Those counts come from a fixed hash sample of the values." if k else ""))}


# ------------------------------------------------------------------ entry points


def profile_files(files: Iterable[tuple[str, bytes | Callable[[], bytes]]]) -> dict[str, Any]:
    """The pure core: names and bytes in, a profile out. No ids, paths or clocks,
    so a golden can pin it. Bytes may be a callable, so skipped files are never read."""
    out: list[dict[str, Any]] = []
    values: dict[str, dict[str, tuple[int, set[str]]]] = {}
    skipped: list[str] = []
    for i, (name, raw) in enumerate(files):
        if i >= MAX_FILES:
            skipped.append(name)
            continue
        data = raw() if callable(raw) else raw
        try:
            f, vals = _profile_file(name, data)
        except csv.Error:
            f, vals = {"name": name, "error": "A field is over the CSV parser limit, or quoting never closes."}, {}
        except UnicodeDecodeError:
            f, vals = {"name": name, "error": "The bytes do not decode under the encoding the file declares."}, {}
        out.append(f)
        values[name] = vals
    return {"files": out, "candidate_joins": _candidate_joins(out, values), "skipped": skipped}


def profile(org_id: str, upload_id: str, log: Callable[[str], Any]) -> dict[str, Any]:
    """The job handler. Writes profile.json, proposal.json and workspace.yaml beside
    the upload. The raw files stay read only, and upload names cannot collide with
    these, because .json and .yaml are not allowed extensions."""
    d = uploads.upload_dir(org_id, upload_id)
    manifest = uploads.manifest(org_id, upload_id)

    def reader(name: str) -> Callable[[], bytes]:
        def read() -> bytes:
            with open(safe_child(d, name, pattern=FILE_NAME), "rb") as fh:
                return fh.read(MAX_BYTES + 1)
        return read

    names = [f["name"] for f in manifest["files"]]
    log(f"profiling {len(names)} files")
    result = profile_files((n, reader(n)) for n in names)
    proposal = propose(result, upload_id)
    write_private(d / "profile.json", json.dumps({"upload_id": upload_id, **result}, indent=1, sort_keys=True))
    write_private(d / "proposal.json", json.dumps(proposal, indent=1, sort_keys=True))
    write_private(d / "workspace.yaml", yaml.safe_dump(proposal["workspace"], sort_keys=False))
    log(f"proposed {len(proposal['workspace']['sources'])} sources with {len(proposal['decisions'])} decisions")
    return {"files": len(result["files"]), "sources": len(proposal["workspace"]["sources"])}
