"""ironwright command line.

  ironwright list                         show packs
  ironwright run --pack retail            everything, end to end
  ironwright run --pack all               every pack
  ironwright generate|migrate|reconcile|ontology|agents|evals|export --pack retail
  ironwright trace --pack retail [--id TRACE_ID]   print a trace as a span tree
  ironwright serve --pack retail          REST API over the ontology
  ironwright mcp --pack retail            MCP server exposing functions and actions as tools
  ironwright worker [--once]              run background jobs from the jobs table

serve and platform bind 127.0.0.1. Anything else needs --host plus --i-know-this-is-exposed.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .packs import list_packs, load_pack
from .tracing import Tracer, load_trace, render_tree


def _save(pack, name: str, data: Any) -> Path:
    p = pack.paths.ensure().reports / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


def _load(pack, name: str) -> Any:
    p = pack.paths.reports / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def stage_generate(pack, tracer, args) -> None:
    if pack.paths.root.exists():
        shutil.rmtree(pack.paths.root)
    pack.paths.ensure()
    with tracer.span("generate.legacy", pack=pack.name):
        gen = pack.generator()
        gt = gen.generate(pack.paths.legacy, seed=args.seed) if args.seed is not None else gen.generate(pack.paths.legacy)
    print(f"  legacy systems written to {pack.paths.legacy}  ({len(gt.get('planted', []))} planted problems)")


def stage_migrate(pack, tracer, args) -> None:
    from .lake import migrate
    rep = migrate(pack, tracer)
    _save(pack, "migrate", rep)
    pub = sum(w["published"] for w in rep["writes"])
    failed = [c for w in rep["writes"] for c in w["checks"] if not c["passed"]]
    print(f"  {pub}/{len(rep['writes'])} tables published, {len(failed)} checks flagged "
          f"({sum(c['severity'] == 'block' for c in failed)} blocking)")


def stage_reconcile(pack, tracer, args) -> None:
    from .reconcile import reconcile
    rep = reconcile(pack, tracer)
    _save(pack, "reconcile", rep)
    for r in rep["rules"]:
        print(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['name']}  {r['counts']}")


def stage_ontology(pack, tracer, args) -> None:
    from .ontology.build import build_ontology
    rep = build_ontology(pack, tracer)
    _save(pack, "ontology", rep)
    for t, v in rep["object_types"].items():
        print(f"  {t:<12} {v['candidates']:>5} source records -> {v['objects']:>5} objects "
              f"({v['merged_clusters']} merged, {v['review_queue']} to review)")


def stage_agents(pack, tracer, args) -> None:
    from .agents.graph import run_usecase
    from .ontology.store import OntologyStore
    s = OntologyStore(pack.paths.ontology_db)
    s.reset_runtime()
    s.con.close()
    if pack.paths.checkpoints.exists():
        pack.paths.checkpoints.unlink()
    out = {}
    for name in pack.usecases.get("usecases", {}):
        rep = run_usecase(pack, name, tracer, limit=args.limit)
        out[name] = rep
        by = {}
        for r in rep["runs"]:
            by[r["diagnosis"]] = by.get(r["diagnosis"], 0) + 1
        print(f"  {name}: {len(rep['runs'])} decisions {by}  outbox {rep['outbox']}")
    _save(pack, "agents", out)


def stage_evals(pack, tracer, args) -> None:
    from .evals import run_evals
    rep = run_evals(pack, tracer)
    _save(pack, "evals", rep)
    for layer in rep["layers"]:
        mark = "PASS" if layer["passed"] else "FAIL"
        print(f"  {mark}  {layer['layer']:<14} {layer['headline']}")


def stage_export(pack, tracer, args) -> None:
    from pathlib import Path

    from .export import export_pack
    out = Path(args.web_dir) / "data" if getattr(args, "web_dir", None) else None
    p = export_pack(pack, out)
    print(f"  snapshot written to {p}")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:      # a name we cannot vouch for, so not loopback
        return False


def _serve(app: Any, args: argparse.Namespace) -> None:
    import uvicorn

    from .api import harden
    # proxy_headers off: uvicorn would otherwise let a loopback peer pick its own
    # client address, and the login limit keys on that address.
    uvicorn.run(harden(app), host=args.host, port=args.port, proxy_headers=False, server_header=False)


STAGES = {"generate": stage_generate, "migrate": stage_migrate, "reconcile": stage_reconcile,
          "ontology": stage_ontology, "agents": stage_agents, "evals": stage_evals, "export": stage_export}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ironwright", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)   # the exposure flag is spelled out in full
    ap.add_argument("command", choices=["list", "run", "trace", "serve", "platform", "auth-set", "mcp", "worker", *STAGES])
    ap.add_argument("--id", dest="trace_id", default=None, help="trace id for the trace command")
    ap.add_argument("--pack", default="retail")
    ap.add_argument("--seed", type=int, default=None, help="generator seed (default: the pack's own)")
    ap.add_argument("--limit", type=int, default=None, help="max agent decisions per use case")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="bind address for serve and platform")
    ap.add_argument("--i-know-this-is-exposed", action="store_true",
                    help="required for any --host that is not loopback")
    ap.add_argument("--web-dir", default="web", help="where export writes data for the web demo")
    ap.add_argument("--once", action="store_true", help="worker: run at most one job, then exit")
    args = ap.parse_args(argv)

    if args.command in ("serve", "platform") and not _is_loopback(args.host):
        if not args.i_know_this_is_exposed:
            ap.error(f"--host {args.host!r} is not loopback; this needs --i-know-this-is-exposed")
        print(f"WARNING: exposed on {args.host}. Read docs/threat-model.md first.", file=sys.stderr)

    if args.command == "list":
        for name in list_packs():
            p = load_pack(name)
            print(f"{name:<14} {p.title:<28} {p.spec.get('industry', '')}")
        return

    if args.command == "worker":
        from .jobs.worker import main as worker_main
        worker_main(once=args.once)
        return

    packs = list_packs() if args.pack == "all" else [args.pack]
    for name in packs:
        pack = load_pack(name)
        tracer = Tracer(pack.paths.ensure().traces) if args.command not in ("trace",) else None
        if args.command == "run":
            print(f"\n== {pack.title} ({name}) ==")
            for stage, run_stage in STAGES.items():
                t0 = time.time()
                print(f"[{stage}]")
                run_stage(pack, tracer, args)
                print(f"  ({time.time() - t0:.1f}s)")
        elif args.command in STAGES:
            STAGES[args.command](pack, tracer, args)
        elif args.command == "trace":
            files = sorted(pack.paths.traces.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            target = next((f for f in files if args.trace_id and f.stem.startswith(args.trace_id)), None)
            if target is None:
                agent_traces = [f for f in files if any(s["name"] == "agent.run" for s in load_trace(f))]
                target = agent_traces[0] if agent_traces else (files[-1] if files else None)
            if not target:
                sys.exit("no traces yet, run the pipeline first")
            print(f"TRACE {target.stem}")
            print(render_tree(load_trace(target)))
        elif args.command == "serve":
            from .api import create_app
            _serve(create_app(name), args)
        elif args.command == "auth-set":
            import getpass

            from .auth import set_account
            email = input("email: ").strip()
            set_account(email, getpass.getpass("password (min 8 chars): "))
            print(f"account set for {email}; hashes live under IRONWRIGHT_DATA/auth/")
        elif args.command == "platform":
            from .platform_api import create_platform_app
            _serve(create_platform_app(), args)
        elif args.command == "mcp":
            from .mcp_server import build_server
            build_server(name).run()


if __name__ == "__main__":
    main()
