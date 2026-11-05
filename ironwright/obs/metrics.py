"""GET /metrics in Prometheus text, behind an operator token.

The API and the worker are separate processes, so a counter in memory here would
never see a job. Everything the worker moves is read from the tables both share, at
scrape time: jobs and queue from `jobs`, tokens from `usage`. Those numbers are right
across restarts and across any number of workers. Only what the API itself sees,
requests and quota refusals, is held in memory, so those are per API process and start
from zero on a restart, which is what rate() expects of a counter.

No client library: the format is a few lines, and this needs no new dependency.
"""
from __future__ import annotations

import hmac
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from ..config import setting
from .logging import emit

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_SOURCES = {"llm_tokens": "platform", "llm_tokens_byo": "byo"}

_lock = threading.Lock()
_requests: Counter[tuple[str, str]] = Counter()
_request_seconds: Counter[str] = Counter()
_quota_refusals: Counter[tuple[str, str]] = Counter()


def authorized(authorization: str) -> bool:
    """Default deny: no token set means nobody. A tenant session is not an operator."""
    want = (setting("METRICS_TOKEN") or "").strip()
    scheme, _, got = authorization.partition(" ")
    return bool(want) and scheme.lower() == "bearer" and hmac.compare_digest(got.strip().encode(), want.encode())


def record_request(route: str, status: int, seconds: float) -> None:
    with _lock:
        _requests[route, str(status)] += 1
        _request_seconds[route] += seconds


def record_quota_refusal(org_id: str | None, route: str) -> None:
    with _lock:
        _quota_refusals[org_id or "unknown", route] += 1


def _escape(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sample(name: str, value: float, **labels: Any) -> str:
    body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
    # Every digit: a rounded counter moves in steps, and rate() reads flat between them.
    shown = str(value) if isinstance(value, int) else repr(float(value))
    return f"{name}{{{body}}} {shown}" if body else f"{name} {shown}"


def _family(name: str, kind: str, help_: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *samples]


def _seconds_between(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def _database_lines() -> list[str]:
    # Imported here: api.py imports this package, and these reach back through tenancy.
    from ..jobs.store import JobStore, jobs
    from ..tenancy import usage

    with JobStore().engine.connect() as con:
        counts = con.execute(sa.select(jobs.c.kind, jobs.c.state, sa.func.count())
                             .group_by(jobs.c.kind, jobs.c.state)).all()
        timed = con.execute(sa.select(jobs.c.kind, jobs.c.state, jobs.c.started_at, jobs.c.finished_at)
                            .where(jobs.c.started_at.is_not(None), jobs.c.finished_at.is_not(None))).all()
        oldest = con.execute(sa.select(sa.func.min(jobs.c.created_at)).where(jobs.c.state == "queued")).scalar()
    with usage.engine().connect() as con:
        tokens = con.execute(sa.select(usage.usage.c.org_id, usage.usage.c.metric, sa.func.sum(usage.usage.c.amount))
                             .where(usage.usage.c.metric.in_(_SOURCES))
                             .group_by(usage.usage.c.org_id, usage.usage.c.metric)).all()

    took_n: Counter[tuple[str, str]] = Counter()
    took_s: Counter[tuple[str, str]] = Counter()
    for kind, state, started, finished in timed:
        took_n[kind, state] += 1
        took_s[kind, state] += _seconds_between(started, finished)
    depth = sum(n for _, state, n in counts if state == "queued")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds() if oldest else 0.0

    return [
        *_family("ironwright_jobs", "gauge", "Jobs now in each state, by kind.",
                 [_sample("ironwright_jobs", n, kind=k, state=s) for k, s, n in sorted(counts)]),
        *_family("ironwright_job_duration_seconds", "summary", "Time from claim to finish, by kind and end state.",
                 [line for (k, s), n in sorted(took_n.items())
                  for line in (_sample("ironwright_job_duration_seconds_count", n, kind=k, state=s),
                               _sample("ironwright_job_duration_seconds_sum", took_s[k, s], kind=k, state=s))]),
        *_family("ironwright_queue_depth", "gauge", "Jobs waiting for a worker.",
                 [_sample("ironwright_queue_depth", depth)]),
        *_family("ironwright_queue_oldest_age_seconds", "gauge", "How long the oldest queued job has waited.",
                 [_sample("ironwright_queue_oldest_age_seconds", max(0.0, age))]),
        *_family("ironwright_llm_tokens_total", "counter",
                 "Model tokens by org. source is platform (our bill) or byo (the org's own key).",
                 [_sample("ironwright_llm_tokens_total", int(n), org_id=o, source=_SOURCES[m])
                  for o, m, n in sorted(tokens)]),
    ]


def render() -> str:
    """Blocking, so run it off the event loop. A dead database still gets an answer:
    that is when an operator most wants the numbers this process does have."""
    with _lock:
        requests, seconds, refusals = Counter(_requests), Counter(_request_seconds), Counter(_quota_refusals)
    lines = [
        *_family("ironwright_requests_total", "counter", "Requests by route template and status.",
                 [_sample("ironwright_requests_total", n, route=r, status=s) for (r, s), n in sorted(requests.items())]),
        *_family("ironwright_request_seconds_total", "counter", "Time spent answering, by route template.",
                 [_sample("ironwright_request_seconds_total", s, route=r) for r, s in sorted(seconds.items())]),
        *_family("ironwright_quota_refusals_total", "counter", "Requests refused with a 429 by a plan limit.",
                 [_sample("ironwright_quota_refusals_total", n, org_id=o, route=r)
                  for (o, r), n in sorted(refusals.items())]),
    ]
    up = 1
    try:
        lines += _database_lines()
    except Exception as e:  # noqa: BLE001  the type only: these messages carry URLs and credentials
        up = 0
        emit("metrics.database_failed", level=logging.WARNING, error_type=type(e).__name__)
    lines += _family("ironwright_metrics_database_up", "gauge", "1 if this scrape could read the database.",
                     [_sample("ironwright_metrics_database_up", up)])
    return "\n".join(lines) + "\n"
