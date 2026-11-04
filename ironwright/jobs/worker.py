"""`ironwright worker`: claim, run, beat, finish, and recover what died.

The heartbeat runs on its own thread, because a stage can block for minutes and
must not look dead while it does. A worker whose heartbeat is refused has lost
the job to a requeue, so it tells the handler to stop, and its late finish is
ignored by the store.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from ..config import private_dir, setting
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
        self.store.requeue_stale(self.heartbeat_timeout)
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
                except Exception:  # noqa: BLE001  a database blip; the timeout judges real death
                    traceback.print_exc(file=sys.stderr)

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
        try:
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
                log(f"failed: {error}")
        except Exception as e:  # noqa: BLE001  the log itself could not be opened
            state, error = "failed", f"{type(e).__name__}: {e}"[:2000]
        finally:
            done.set()
            beater.join(5)
        self.store.finish(job_id, token, state, error=error, result=result)

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.run_once() is None:
                    stop.wait(self.idle)
            except Exception:  # noqa: BLE001  keep serving; a dead database comes back
                traceback.print_exc(file=sys.stderr)
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
