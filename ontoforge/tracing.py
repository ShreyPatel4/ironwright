"""Small OpenTelemetry-shaped tracer.

Spans nest, carry attributes and timestamped events, and can link to other traces
(for example a resume after a human approval). Each trace is written as JSON lines,
so it can be shipped to any OTel backend later or rendered by the web demo.
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("span", default=None)

# keys never written to traces, so sensitive values stay in the governed store
REDACT_KEYS = {"ssn", "dob", "account_number", "card_number", "email", "phone"}


def _redact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if k.lower() in REDACT_KEYS else v) for k, v in attrs.items()}


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    status: str = "ok"

    def set(self, **attrs: Any) -> "Span":
        self.attributes.update(_redact(attrs))
        return self

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "ts": time.time(), "attributes": _redact(attrs)})

    def link(self, trace_id: str, reason: str) -> None:
        self.links.append({"trace_id": trace_id, "reason": reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "trace_id": self.trace_id, "span_id": self.span_id,
            "parent_id": self.parent_id, "start": self.start, "end": self.end,
            "duration_ms": round(((self.end or time.time()) - self.start) * 1000, 2),
            "attributes": self.attributes, "events": self.events, "links": self.links,
            "status": self.status,
        }


class Tracer:
    def __init__(self, out_dir: Path | None):
        self.out_dir = out_dir
        self.spans: list[Span] = []

    @contextmanager
    def span(self, name: str, trace_id: str | None = None, **attrs: Any) -> Iterator[Span]:
        parent = _current.get()
        tid = trace_id or (parent.trace_id if parent else uuid.uuid4().hex[:16])
        s = Span(name=name, trace_id=tid, span_id=uuid.uuid4().hex[:12],
                 parent_id=parent.span_id if parent and parent.trace_id == tid else None,
                 start=time.time(), attributes=_redact(attrs))
        token = _current.set(s)
        try:
            yield s
        except Exception as e:  # recorded, then re-raised
            if type(e).__name__ == "GraphInterrupt":   # a pause for a human, not a failure
                s.event("interrupt", reason="waiting for human review")
            else:
                s.status = "error"
                s.event("exception", type=type(e).__name__, message=str(e)[:300])
            raise
        finally:
            s.end = time.time()
            _current.reset(token)
            self.spans.append(s)
            if self.out_dir:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                with open(self.out_dir / f"{s.trace_id}.jsonl", "a") as f:
                    f.write(json.dumps(s.to_dict(), default=str) + "\n")

    @staticmethod
    def current() -> Span | None:
        return _current.get()


def load_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render_tree(spans: list[dict[str, Any]]) -> str:
    """Dense text tree of one trace, like the waterfall in the prep guide."""
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        children.setdefault(s["parent_id"], []).append(s)
    for v in children.values():
        v.sort(key=lambda s: s["start"])
    lines: list[str] = []

    def walk(parent: str | None, prefix: str) -> None:
        kids = children.get(parent, [])
        for i, s in enumerate(kids):
            last = i == len(kids) - 1
            attrs = " ".join(f"{k}={v}" for k, v in list(s["attributes"].items())[:4])
            lines.append(f"{prefix}{'└─' if last else '├─'} {s['name']}  {s['duration_ms']:.0f} ms  {attrs}".rstrip())
            for e in s["events"]:
                ea = " ".join(f"{k}={v}" for k, v in e["attributes"].items())
                lines.append(f"{prefix}{'   ' if last else '│  '}· event {e['name']} {ea}".rstrip())
            walk(s["span_id"], prefix + ("   " if last else "│  "))

    walk(None, "")
    return "\n".join(lines)
