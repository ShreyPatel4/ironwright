"""Slice B gate (sketch 08, op_otel): OTLP/HTTP export of workspace traces.

An in-process fixture collector (a tiny http server thread) captures every
POSTed ExportTraceServiceRequest, so the gate replays offline with no docker
and no real backend. Every span on disk must arrive, trace ids must
round-trip as consistent hex, and parent/child relationships must survive.
"""
import http.server
import json
import os
import socket
import threading

import pytest
from fastapi.testclient import TestClient


class Collector:
    """Captures OTLP/HTTP JSON posts; the smallest honest stand-in for a backend."""

    def __init__(self):
        self.requests: list[dict] = []
        bucket = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                bucket.append({"path": self.path,
                               "body": json.loads(self.rfile.read(length))})
                payload = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def spans(self) -> list[dict]:
        out = []
        for req in self.requests:
            for rs in req["body"]["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return out

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("otel")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        from ironwright.otel_export import router
        from ironwright.platform_api import create_platform_app

        app = create_platform_app()
        app.include_router(router)
        with TestClient(app) as c:
            r = c.post("/workspaces/retail/runs", json={"seed": 7})
            assert r.status_code == 200, r.text
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


@pytest.fixture(scope="module")
def collector():
    c = Collector()
    yield c
    c.close()


def _disk_traces() -> dict[str, list[dict]]:
    from ironwright.packs import load_pack

    tdir = load_pack("retail").paths.traces
    out = {}
    for f in sorted(tdir.glob("*.jsonl")):
        out[f.stem] = [json.loads(line) for line in f.read_text().splitlines()
                       if line.strip()]
    return out


def test_every_span_reaches_the_collector(built, collector):
    disk = _disk_traces()
    disk_span_count = sum(len(v) for v in disk.values())
    assert disk_span_count > 0, "a built workspace must have traces"

    r = built.post("/workspaces/retail/otel/export",
                   json={"endpoint": collector.endpoint})
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts["traces"] == len(disk)
    assert counts["spans"] == disk_span_count
    assert len(collector.spans()) == disk_span_count
    assert all(req["path"] == "/v1/traces" for req in collector.requests)


def test_trace_ids_round_trip_as_consistent_hex(built, collector):
    from ironwright.otel_export import span_id_hex, trace_id_hex

    disk = _disk_traces()
    received = {s["traceId"] for s in collector.spans()}
    assert received == {trace_id_hex(tid) for tid in disk}
    for tid in received:
        assert len(tid) == 32 and set(tid) <= set("0123456789abcdef")
    hexdigits = set("0123456789abcdef")
    for s in collector.spans():
        assert len(s["spanId"]) == 16 and set(s["spanId"]) <= hexdigits

    # the mapping is deterministic: every disk span id lands on the wire
    wire_ids = {s["spanId"] for s in collector.spans()}
    for spans in disk.values():
        for s in spans:
            assert span_id_hex(s["span_id"]) in wire_ids


def test_parent_child_relationships_survive(built, collector):
    from ironwright.otel_export import span_id_hex, trace_id_hex

    disk = _disk_traces()
    pair = None
    for tid, spans in disk.items():
        ids = {s["span_id"]: s for s in spans}
        for s in spans:
            if s["parent_id"] and s["parent_id"] in ids:
                pair = (tid, ids[s["parent_id"]], s)
                break
        if pair:
            break
    assert pair, "a built retail workspace must contain at least one nested span"
    tid, parent, child = pair

    wire = [s for s in collector.spans() if s["traceId"] == trace_id_hex(tid)]
    assert len(wire) == len(disk[tid])
    wire_child = next(s for s in wire if s["spanId"] == span_id_hex(child["span_id"]))
    assert wire_child["parentSpanId"] == span_id_hex(parent["span_id"])
    assert wire_child["name"] == child["name"]
    assert int(wire_child["startTimeUnixNano"]) == int(child["start"] * 1e9)
    assert int(wire_child["endTimeUnixNano"]) >= int(wire_child["startTimeUnixNano"])


def test_module_is_callable_as_a_main(built, collector, capsys):
    from ironwright.otel_export import main

    before = len(collector.requests)
    main(["retail", collector.endpoint])
    printed = json.loads(capsys.readouterr().out.strip())
    disk = _disk_traces()
    assert printed["traces"] == len(disk)
    assert printed["spans"] == sum(len(v) for v in disk.values())
    assert len(collector.requests) == before + len(disk)


def test_unreachable_collector_is_a_502_with_the_reason(built):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    r = built.post("/workspaces/retail/otel/export",
                   json={"endpoint": f"http://127.0.0.1:{dead_port}"})
    assert r.status_code == 502
    assert "collector" in r.json()["detail"]
    assert built.post("/workspaces/nonsense/otel/export",
                      json={"endpoint": "http://127.0.0.1:1"}).status_code == 404
