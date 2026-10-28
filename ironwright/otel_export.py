"""OTLP/HTTP export of workspace traces (sketch 08, op_otel).

Spans are already OTel-shaped dicts on disk, one JSON line per span in
<data>/<pack>/traces/<trace_id>.jsonl (see tracing.Span.to_dict). This module
maps those fields onto the OTLP/HTTP JSON ExportTraceServiceRequest shape
(resourceSpans -> scopeSpans -> spans) and posts one request per trace with
httpx. Our ids are shorter than the wire format (16 hex trace, 12 hex span),
so they are zero-padded to 32 and 16; the mapping is deterministic, so ids
stay consistent across exports and parent links keep pointing at the right
span.

Callable without the CLI:

    python -m ironwright.otel_export <workspace> <endpoint>

and mountable next to the other platform routes:

    from ironwright.otel_export import router as otel_router
    app.include_router(otel_router)
"""
from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .packs import Pack, list_packs, load_pack
from .tracing import load_trace

router = APIRouter()

_HEX = set("0123456789abcdef")
_STATUS_CODES = {"ok": 1, "error": 2}  # OTLP: STATUS_CODE_OK, STATUS_CODE_ERROR
_SPAN_KIND_INTERNAL = 1


def _hex_id(raw: Any, width: int) -> str:
    """A deterministic wire id: zero-pad our shorter hex ids, hash anything
    that is not clean hex. Same input, same output, every export."""
    s = str(raw).lower()
    if 0 < len(s) <= width and set(s) <= _HEX:
        return s.zfill(width)
    return hashlib.sha256(s.encode()).hexdigest()[:width]


def trace_id_hex(trace_id: Any) -> str:
    return _hex_id(trace_id, 32)


def span_id_hex(span_id: Any) -> str:
    return _hex_id(span_id, 16)


def _any_value(v: Any) -> dict[str, Any]:
    """Our attribute values onto OTLP AnyValue. int64 values travel as
    strings per the proto3 JSON mapping; structures are serialized, which
    keeps the export honest without inventing nested AnyValue trees."""
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if v is None or isinstance(v, str):
        return {"stringValue": v or ""}
    return {"stringValue": json.dumps(v, default=str, sort_keys=True)}


def _attributes(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _any_value(v)} for k, v in attrs.items()]


def _nanos(seconds: float) -> str:
    return str(int(seconds * 1e9))


def span_to_otlp(span: dict[str, Any]) -> dict[str, Any]:
    """One of our span dicts onto one OTLP JSON Span."""
    out: dict[str, Any] = {
        "traceId": trace_id_hex(span["trace_id"]),
        "spanId": span_id_hex(span["span_id"]),
        "name": span["name"],
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": _nanos(span["start"]),
        "endTimeUnixNano": _nanos(span["end"] if span.get("end") else span["start"]),
        "attributes": _attributes(span.get("attributes", {})),
        "status": {"code": _STATUS_CODES.get(span.get("status", "ok"), 1)},
    }
    if span.get("parent_id"):
        out["parentSpanId"] = span_id_hex(span["parent_id"])
    if span.get("events"):
        out["events"] = [{"timeUnixNano": _nanos(e["ts"]), "name": e["name"],
                          "attributes": _attributes(e.get("attributes", {}))}
                         for e in span["events"]]
    if span.get("links"):
        # our links carry a trace id and a reason, never a span id. The wire
        # format requires a non-zero spanId on a link and strict collectors
        # drop zero-id links, so these travel as events instead: the linked
        # trace id and reason survive in any collector.
        out.setdefault("events", []).extend(
            {"timeUnixNano": _nanos(span["start"]), "name": "linked_trace",
             "attributes": _attributes({"trace_id": trace_id_hex(lk["trace_id"]),
                                        "reason": lk.get("reason", "")})}
            for lk in span["links"])
    return out


def trace_request(workspace: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """One ExportTraceServiceRequest for one trace."""
    return {"resourceSpans": [{
        "resource": {"attributes": _attributes({"service.name": f"ironwright.{workspace}"})},
        "scopeSpans": [{"scope": {"name": "ironwright.tracing"},
                        "spans": [span_to_otlp(s) for s in spans]}],
    }]}


def _traces_url(endpoint: str) -> str:
    url = endpoint.rstrip("/")
    return url if url.endswith("/v1/traces") else url + "/v1/traces"


def export_workspace(pack: Pack, endpoint: str) -> dict[str, Any]:
    """Post every trace of one workspace to an OTLP/HTTP collector.

    One POST per trace file. Raises httpx.HTTPError when the collector is
    unreachable or rejects a request, so callers can report the reason
    instead of counting a failed export as done."""
    url = _traces_url(endpoint)
    traces = sorted(pack.paths.traces.glob("*.jsonl")) if pack.paths.traces.exists() else []
    counts = {"traces": 0, "spans": 0, "endpoint": url}
    with httpx.Client(timeout=10.0) as client:
        for path in traces:
            spans = load_trace(path)
            if not spans:
                continue
            r = client.post(url, json=trace_request(pack.name, spans),
                            headers={"Content-Type": "application/json"})
            r.raise_for_status()
            counts["traces"] += 1
            counts["spans"] += len(spans)
    return counts


class ExportBody(BaseModel):
    endpoint: str


@router.post("/workspaces/{name}/otel/export")
def otel_export(name: str, body: ExportBody) -> dict[str, Any]:
    if name not in list_packs():
        raise HTTPException(404, f"no workspace named {name}")
    try:
        return export_workspace(load_pack(name), body.endpoint)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"collector unreachable or rejected the export: "
                                 f"{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m ironwright.otel_export",
        description="Post every trace of one workspace to an OTLP/HTTP collector.")
    ap.add_argument("workspace", help="pack name, e.g. retail")
    ap.add_argument("endpoint", help="collector base URL or full /v1/traces URL")
    args = ap.parse_args(argv)
    if args.workspace not in list_packs():
        raise SystemExit(f"no workspace named {args.workspace}")
    counts = export_workspace(load_pack(args.workspace), args.endpoint)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
