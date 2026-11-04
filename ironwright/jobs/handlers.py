"""What a job does, by kind: @job("run") def run(ctx, workspace, params, log).

A handler polls ctx.check_cancel() at safe points and writes through log(). The
log callable is per job, so concurrent jobs of different orgs never mix. Stage
print() output stays on the worker's stdout for that reason.
"""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import verification
from ..packs import list_packs, load_pack
from ..tracing import Tracer
from .store import DEV_ORG

STAGE_ORDER = ["generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"]
DEFAULT_SEEDS = {"retail": 7, "supply_chain": 11, "banking_aml": 23, "insurance_claims": 31}
HANDLERS: dict[str, Callable[..., Any]] = {}


class JobCancelled(Exception):
    """Raised by check_cancel. The worker turns it into state cancelled."""


class JobError(Exception):
    """A failure the job's owner should read as is."""


@dataclass(frozen=True)
class JobCtx:
    """What T1's Ctx carries that a worker can know, plus the job itself."""
    org_id: str
    principal: str
    job_id: str
    cancelled: Callable[[], bool]

    def check_cancel(self) -> None:
        if self.cancelled():
            raise JobCancelled()


def job(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        if kind in HANDLERS:
            raise ValueError(f"a handler for {kind} is already registered")
        HANDLERS[kind] = fn
        return fn
    return register


def pipeline_stages() -> dict[str, Callable[..., None]]:
    from ..cli import STAGES
    return STAGES


def derive_verification(pack: Any) -> dict[str, Any]:
    return verification.derive(pack)


def validate_params(kind: str, params: dict[str, Any]) -> None:
    """Refuse at the door what could only fail later. Raises ValueError."""
    if kind != "run":
        return
    stages = params.get("stages")
    if stages is not None:
        if not isinstance(stages, list):
            raise ValueError("stages must be a list")
        bad = [s for s in stages if s not in STAGE_ORDER]
        if bad:
            raise ValueError(f"unknown stages {bad}")
    seed = params.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ValueError("seed must be an integer")


def _load(org_id: str, workspace: str) -> Any:
    """The dev org runs the demo packs. Every other org runs its own workspaces
    and sees a demo pack as read only (contract 2): generate wipes the pack's
    folder, and that folder is shared."""
    if org_id == DEV_ORG and workspace in list_packs():
        return load_pack(workspace)
    try:
        from ..workspaces_tenant import load_workspace  # T2
    except ImportError:
        load_workspace = None
    if load_workspace is not None:
        try:
            return load_workspace(org_id, workspace)
        except FileNotFoundError:
            pass
    if workspace in list_packs():
        raise JobError(f"{workspace} is a demo pack, read only to this org")
    raise JobError(f"no workspace named {workspace}")


@job("run")
def run(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> dict[str, Any]:
    """The whole pipeline, or the stages asked for. Cancel lands between stages:
    a stage holds a DuckDB or Iceberg write, and stopping inside one tears it."""
    pack = _load(ctx.org_id, workspace)
    available = pipeline_stages()
    if pack.spec.get("kind") == "workspace":  # no generator: sources are uploads
        available = {k: v for k, v in available.items() if k != "generate"}
    stages = params.get("stages") or list(available)
    missing = [s for s in stages if s not in available]
    if missing:
        raise JobError(f"this workspace has no stage {missing}")
    seed = params.get("seed")
    args = argparse.Namespace(seed=seed if seed is not None else DEFAULT_SEEDS.get(workspace, 7),
                              limit=None)
    tracer = Tracer(pack.paths.ensure().traces)
    report = []
    for stage in stages:
        ctx.check_cancel()
        log(f"[{stage}] started")
        t0 = time.perf_counter()
        available[stage](pack, tracer, args)
        seconds = round(time.perf_counter() - t0, 2)
        log(f"[{stage}] ok in {seconds}s")
        report.append({"stage": stage, "status": "ok", "seconds": seconds})
    evals_path = pack.paths.reports / "evals.json"
    evals = json.loads(evals_path.read_text()) if evals_path.exists() else None
    return {"workspace": workspace, "stages": report,
            "evals": {"passed": evals.get("passed")} if evals else None,
            "verification": derive_verification(pack)}


@job("profile")
def profile(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    raise JobError("profile is not built yet (T3 fills this)")


@job("ontology_propose")
def ontology_propose(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    raise JobError("ontology_propose is not built yet (wave 3 fills this)")
