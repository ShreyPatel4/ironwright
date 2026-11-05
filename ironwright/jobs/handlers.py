"""What a job does, by kind: @job("run") def run(ctx, workspace, params, log).

A handler polls ctx.check_cancel() at safe points and writes through log(). The
log callable is per job, so concurrent jobs of different orgs never mix. Stage
print() output stays on the worker's stdout for that reason.
"""
from __future__ import annotations

import argparse
import contextvars
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import auth, verification
from .. import workspaces_tenant as wt
from ..config import UnsafePath
from ..ingest import profiler
from ..packs import list_packs, load_pack
from ..tenancy import DEV_ORG_ID, quotas
from ..tenancy import service as tenancy
from ..tracing import Tracer

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
    mode = params.get("mode")
    if mode is not None and mode not in wt.RUN_MODES:
        raise ValueError(f"mode must be one of {', '.join(wt.RUN_MODES)}")


def _load(org_id: str, workspace: str) -> Any:
    """The dev org runs the demo packs. Every other org runs its own workspaces
    and sees a demo pack as read only (contract 2): generate wipes the pack's
    folder, and that folder is shared."""
    if org_id == DEV_ORG_ID and workspace in list_packs():
        return load_pack(workspace)
    try:
        return wt.load_workspace(org_id, workspace)
    except FileNotFoundError:
        pass
    if workspace in list_packs():
        raise JobError(f"{workspace} is a demo pack, read only to this org")
    raise JobError(f"no workspace named {workspace}")


def _run_tenant(ctx: JobCtx, pack: Any, params: dict[str, Any], log: Callable[[str], None]) -> dict[str, Any]:
    """T2's run, not the demo stages: no generate, no agents, its own eval layers,
    and it never wipes run/. It is one call, so cancel lands before it or not at all."""
    if params.get("stages"):
        raise JobError("a tenant workspace runs as a whole; stages cannot be chosen")
    mode = params.get("mode", "append")
    ctx.check_cancel()
    log(f"[{pack.name}] started, mode {mode}")
    t0 = time.perf_counter()
    try:
        report = wt.run_workspace(ctx.org_id, pack.name, mode=mode)
    except wt.DriftBlocked as e:
        log(f"[{pack.name}] stopped by drift")
        raise JobError(str(e)) from e
    log(f"[{pack.name}] done in {round(time.perf_counter() - t0, 2)}s, evals passed: {report['passed']}")
    return {"workspace": pack.name, "stages": [], "mode": mode, "evals": {"passed": report["passed"]},
            "layers": report["layers"], "verification": derive_verification(pack)}


@job("run")
def run(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> dict[str, Any]:
    """The whole pipeline, or the stages asked for. Cancel lands between stages:
    a stage holds a DuckDB or Iceberg write, and stopping inside one tears it."""
    pack = _load(ctx.org_id, workspace)
    # One job, one trace: every span under this root inherits the job id as its trace id.
    tracer = Tracer(pack.paths.ensure().traces)
    with tracer.span("job.run", trace_id=ctx.job_id, **{"job.kind": "run", "org_id": ctx.org_id}):
        if pack.is_tenant:
            return _run_tenant(ctx, pack, params, log)
        if params.get("mode"):
            raise JobError("run modes are for tenant workspaces; a demo pack regenerates from scratch")
        return _run_stages(ctx, pack, workspace, params, log, tracer)


def _run_stages(ctx: JobCtx, pack: Any, workspace: str, params: dict[str, Any], log: Callable[[str], None],
                tracer: Tracer) -> dict[str, Any]:
    available = pipeline_stages()
    stages = params.get("stages") or list(available)
    missing = [s for s in stages if s not in available]
    if missing:
        raise JobError(f"this workspace has no stage {missing}")
    seed = params.get("seed")
    args = argparse.Namespace(seed=seed if seed is not None else DEFAULT_SEEDS.get(workspace, 7),
                              limit=None)
    report = []
    for stage in stages:
        ctx.check_cancel()
        log(f"[{stage}] started")
        t0 = time.perf_counter()
        with tracer.span(f"stage.{stage}"):
            if stage == "agents":
                # Each decision is its own trace: export.py and the console read them one
                # by one. An empty context keeps them from inheriting the job as a parent.
                contextvars.Context().run(available[stage], pack, tracer, args)
            else:
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
    """Contract 5. The upload id comes from the body, and the org from the job, so
    another org's id answers exactly what an id nobody has answers. One call, so
    cancel lands before it or not at all."""
    upload_id = params.get("upload_id")
    if not isinstance(upload_id, str):
        raise JobError("profile needs params.upload_id")
    ctx.check_cancel()
    try:
        return profiler.profile(ctx.org_id, upload_id, log)
    except (UnsafePath, FileNotFoundError) as e:
        raise JobError("no such upload") from e


@job("silver_draft")
def silver_draft(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    """Drafts silver again from the latest upload of each source. A member may start this,
    as any job kind, so it only keeps a draft. An admin route turns a draft into models."""
    from ..ingest import silver_draft as drafter

    pack = _load(ctx.org_id, workspace)
    if not pack.is_tenant:
        raise JobError(f"{workspace} is a demo pack and ships its own models")
    ctx.check_cancel()
    return drafter.redraft(ctx.org_id, workspace, log)


@job("ontology_propose")
def ontology_propose(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    from ..ontology import propose as proposer

    pack = _load(ctx.org_id, workspace)
    if not pack.is_tenant:
        raise JobError(f"{workspace} is a demo pack and ships its own ontology")
    ctx.check_cancel()
    try:
        llm = (quotas.narration_llm(ctx.org_id, auth._now()) or proposer.LLM()) if params.get("narrate") else None
        made = proposer.store_proposal(pack, llm=llm)
    except proposer.ProposalError as e:
        raise JobError(str(e)) from e
    log(f"[{workspace}] proposed {len(made['objects'])} objects, {len(made['links'])} links")
    return {"workspace": workspace, "objects": len(made["objects"]), "links": len(made["links"]),
            "functions": len(made["functions"])}


@job("org_export")
def org_export(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    """Owner only. /jobs takes any registered kind from a member, so the service reads the role."""
    return tenancy.export_org(ctx.org_id, ctx.principal, ctx.job_id, ctx.check_cancel, log, auth._now())


@job("org_delete")
def org_delete(ctx: JobCtx, workspace: str, params: dict[str, Any], log: Callable[[str], None]) -> Any:
    """Does nothing unless the owner route marked the org. No cancel check and no log
    line once it starts: nobody is left to cancel it, and the log folder goes with the org."""
    return tenancy.delete_org_data(ctx.org_id, auth._now())
