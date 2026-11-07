"""`ironwright worker`: claim, run, beat, finish, and recover what died.

The heartbeat runs on its own thread, because a stage can block for minutes and
must not look dead while it does. A worker whose heartbeat is refused has lost
the job to a requeue, so it tells the handler to stop, and its late finish is
ignored by the store.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from .. import durable, otel_export
from ..config import private_dir, setting
from ..obs.logging import emit
from . import handlers
from .store import JobStore, _now, log_file, worker_cap

CANCEL_POLL_SECONDS = 0.5  # a handler may poll in a tight loop; the database need not see it


def _job_log(org_id: str, job_id: str) -> Callable[[str], None]:
    path = log_file(org_id, job_id)
    private_dir(path.parent)

    def write(line: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"{_now()} {line}\n")

    return write


def _worker_error(event: str, e: Exception) -> None:
    """File and line, never the message: a database error quotes the row it choked on."""
    emit(event, level=logging.ERROR, error_type=type(e).__name__, stack=traceback.format_tb(e.__traceback__)[-5:])


def _job_fields(got: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": got["id"], "org_id": got["org_id"], "workspace": got["workspace"], "kind": got["kind"],
            "user_id": got["created_by"].removeprefix("user:")}


class Worker:
    def __init__(self, store: JobStore | None = None, heartbeat_interval: float | None = None,
                 heartbeat_timeout: float | None = None, idle: float = 1.0) -> None:
        self.store = store or JobStore()
        self.heartbeat_interval = (float(setting("JOB_HEARTBEAT_SECONDS", "10"))
                                   if heartbeat_interval is None else heartbeat_interval)
        self.heartbeat_timeout = (float(setting("JOB_HEARTBEAT_TIMEOUT", "60"))
                                  if heartbeat_timeout is None else heartbeat_timeout)
        self.idle = idle
        self.name = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def run_once(self, job_id: str | None = None) -> dict[str, Any] | None:
        """Recover the dead, then run one job to its end. None if nothing could start."""
        for requeued in self.store.requeue_stale(self.heartbeat_timeout):
            emit("job.requeued", level=logging.WARNING, job_id=requeued)
        got = self.store.claim(self.name, job_id=job_id)
        if got is None:
            return None
        self._execute(got)
        return got

    def _execute(self, got: dict[str, Any]) -> None:
        job_id, token = got["id"], got["token"]
        lost = threading.Event()
        done = threading.Event()

        def beat() -> None:
            while not done.wait(self.heartbeat_interval):
                try:
                    if not self.store.heartbeat(job_id, token):
                        lost.set()
                        return
                except Exception as e:  # noqa: BLE001  a database blip; the timeout judges real death
                    _worker_error("worker.heartbeat_failed", e)

        polled = [0.0, False]  # when, answer

        def cancelled() -> bool:
            if lost.is_set():
                return True
            if time.monotonic() - polled[0] >= CANCEL_POLL_SECONDS:
                polled[:] = [time.monotonic(), self.store.cancel_requested(job_id)]
            return polled[1]

        beater = threading.Thread(target=beat, daemon=True, name=f"beat-{job_id[:8]}")
        beater.start()
        state, error, result = "succeeded", None, None
        error_type = None
        started = time.perf_counter()
        emit("job.started", **_job_fields(got))
        try:
            # The job owns its org until it has persisted: hydrate first, persist last.
            with durable.job_scope(got["org_id"]):
                log = _job_log(got["org_id"], job_id)
                try:
                    handler = handlers.HANDLERS.get(got["kind"])
                    if handler is None:
                        raise handlers.JobError(f"no handler for kind {got['kind']}")
                    ctx = handlers.JobCtx(org_id=got["org_id"], principal=got["created_by"],
                                          job_id=job_id, cancelled=cancelled)
                    result = handler(ctx, got["workspace"], got["params"], log)
                except handlers.JobCancelled:
                    state = "cancelled"
                    log("cancelled")
                except Exception as e:  # noqa: BLE001  any handler failure is the job's, not the worker's
                    state, error = "failed", f"{type(e).__name__}: {e}"[:2000]
                    error_type = type(e).__name__
                    log(f"failed: {error}")
                durable.after_job(got["org_id"], got["kind"], state)
        except Exception as e:  # noqa: BLE001  the log could not be opened, or the org could not be hydrated
            state, error = "failed", f"{type(e).__name__}: {e}"[:2000]
            error_type = type(e).__name__
        finally:
            done.set()
            beater.join(5)
        owned = self.store.finish(job_id, token, state, error=error, result=result)
        # The org reads the message through /jobs/{id}. This line gets the type.
        emit("job.finished", **_job_fields(got), state=state, error_type=error_type, owned=owned,
             duration_ms=round((time.perf_counter() - started) * 1000, 2))
        if owned:
            self._export_trace(got)

    @staticmethod
    def _export_trace(got: dict[str, Any]) -> None:
        """After the finish, so a slow or dead collector costs the job nothing."""
        endpoint = otel_export.configured_endpoint()
        if got["kind"] != "run" or endpoint is None:
            return
        try:
            traces = handlers._load(got["org_id"], got["workspace"]).paths.traces
            otel_export.export_job_trace(got["id"], traces, endpoint)
        except Exception as e:  # noqa: BLE001  the type only: httpx messages name the endpoint
            emit("trace.export_failed", level=logging.WARNING, job_id=got["id"], org_id=got["org_id"],
                 error_type=type(e).__name__)

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.run_once() is None:
                    stop.wait(self.idle)
            except Exception as e:  # noqa: BLE001  keep serving; a dead database comes back
                _worker_error("worker.loop_failed", e)
                stop.wait(self.idle)

    def run(self, stop: threading.Event) -> None:
        """Up to the cap of threads here. The cap itself is global, in the claim,
        so more worker processes add capacity only up to IRONWRIGHT_WORKERS."""
        threads = [threading.Thread(target=self._loop, args=(stop,), name=f"job-{n}")
                   for n in range(worker_cap())]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


def main(once: bool = False) -> None:
    worker = Worker()
    if once:
        worker.run_once()
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    print(f"worker {worker.name}: up to {worker_cap()} jobs at once", flush=True)
    worker.run(stop)
