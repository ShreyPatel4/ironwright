"""deploy/cloudflare: the Worker, the container config and the deploy workflow.

Nothing here talks to Cloudflare. The Worker's edge logic runs under node with a fake
container (deploy/cloudflare/test.mjs). The rest reads the files and holds them to what the
runbook promises: one named instance, both processes, the prefix, no secret in a file, and a
workflow that only a person can start.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "deploy" / "cloudflare"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudflare.yml"
PREFIX = "/projects/ironwright/app"


def _jsonc(path: Path) -> dict:
    text = re.sub(r"(?m)^\s*//.*$", "", path.read_text())
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


@pytest.fixture(scope="module")
def wrangler() -> dict:
    return _jsonc(KIT / "wrangler.jsonc")


def test_the_worker_edge_logic_passes_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found, so the Worker logic was not proved here")
    out = subprocess.run([node, str(KIT / "test.mjs")], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stdout + out.stderr


def test_one_worker_on_workers_dev(wrangler):
    assert wrangler["name"] == "ironwright-platform"
    assert wrangler["workers_dev"] is True
    assert "routes" not in wrangler and "route" not in wrangler, "the zone is DNS only, a route never fires"


def test_the_container_is_the_repo_image_and_only_one_runs(wrangler):
    (c,) = wrangler["containers"]
    assert (KIT / c["image"]).resolve() == ROOT / "Dockerfile"
    assert (KIT / c["image_build_context"]).resolve() == ROOT
    assert c["max_instances"] == 1
    # The journey runs a full pipeline in the worker beside the API. lite and basic run out of memory.
    assert c["instance_type"] in ("standard-2", "standard-3", "standard-4")
    (binding,) = wrangler["durable_objects"]["bindings"]
    assert binding["class_name"] == c["class_name"]
    assert any(c["class_name"] in m.get("new_sqlite_classes", []) for m in wrangler["migrations"])


def test_the_vars_say_prod_under_the_prefix_and_hold_no_secret(wrangler):
    v = wrangler["vars"]
    assert v["IRONWRIGHT_ENV"] == "prod"
    assert v["IRONWRIGHT_BASE_PATH"] == PREFIX
    assert v["IRONWRIGHT_CONSOLE_ORIGIN"] == "https://coconutlabs.org"
    assert v["IRONWRIGHT_SIGNUP"] == "invite"
    assert v["IRONWRIGHT_DEMO_ORG"]
    for key in v:
        assert not re.search(r"SECRET|PASSWORD|TOKEN|ACCESS_KEY|DB_URL", key), f"{key} belongs in wrangler secret put"


def _index() -> str:
    return (KIT / "src" / "index.js").read_text() + (KIT / "src" / "edge.js").read_text()


def test_one_named_instance_and_both_processes():
    src = _index()
    assert 'getByName("main")' in src
    assert "ironwright worker" in src and "ironwright serve-platform" in src
    assert "wait -n" in src, "if either process dies the container exits and comes back whole"
    assert "ironwright run --pack all" in src, "a fresh disk has no demo packs until they are built"


def test_a_demo_session_never_cold_starts_mid_walk():
    m = re.search(r'sleepAfter\s*=\s*"(\d+)([mh])"', _index())
    assert m, "sleepAfter must be a duration string"
    minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    assert minutes >= 60


def test_every_secret_the_runbook_names_is_passed_to_the_container():
    src, readme = _index(), (KIT / "README.md").read_text()
    secrets = set(re.findall(r"wrangler secret put (\w+)", readme))
    assert {"IRONWRIGHT_SECRET_KEY", "IRONWRIGHT_DB_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= secrets
    for name in secrets:
        assert name in src, f"{name} is set on the Worker but never reaches the container"


def test_the_runbook_has_the_checklist_in_order_and_a_diagram():
    readme = (KIT / "README.md").read_text()
    order = ["wrangler login", "Workers Paid", "Neon", "ironwright-state", "wrangler secret put", "wrangler deploy",
             "hosted_smoke.py", "rollback"]
    steps = readme[readme.index("## The checklist"):].lower()
    at = [steps.find(step.lower()) for step in order]
    assert -1 not in at, dict(zip(order, at))
    assert at == sorted(at), dict(zip(order, at))
    assert "```mermaid" in readme
    assert "—" not in readme


def test_the_site_patch_adds_both_rewrites_and_a_try_it_link():
    patch = (KIT / "coconutlabs-site.patch").read_text()
    host = "https://ironwright-platform.shrey77-wrk.workers.dev"
    assert f'source: "{PREFIX}", destination: "{host}{PREFIX}"' in patch
    assert f'source: "{PREFIX}/:path*", destination: "{host}{PREFIX}/:path*"' in patch
    assert "app/projects/ironwright/page.tsx" in patch and "Try it" in patch
    assert "—" not in patch


def test_the_deploy_workflow_only_runs_when_a_person_starts_it():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))  # yaml reads a bare `on:` as True
    assert set(on) == {"workflow_dispatch"}
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    deploy = next(s for s in steps if "wrangler" in json.dumps(s) and "deploy" in json.dumps(s))
    assert "CLOUDFLARE_API_TOKEN" in json.dumps(deploy)
    assert "deploy/cloudflare" in json.dumps(deploy)
    assert any("hosted_smoke.py" in s.get("run", "") for s in steps)


def test_ci_never_deploys_the_platform():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "deploy/cloudflare" not in ci.replace("make cf-check", "")


def test_ci_runs_cf_check():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = [s.get("run", "") for job in ci["jobs"].values() for s in job["steps"]]
    assert any("make cf-check" in r for r in runs)


def test_the_dead_route_worker_is_gone():
    assert not (ROOT / "deploy" / "route-worker").exists()
    for path in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / "CLAUDE.md", ROOT / "deploy" / "README.md"):
        assert "route-worker" not in path.read_text(), path


# ---------------------------------------------------------------- the start command, run for real
STUB = """#!/bin/sh
echo "$1" >> "$IW_LOG"
case "$1" in
  worker) sleep "$WORKER_LIFE" ;;
  serve-platform) sleep "$API_LIFE" ;;
  run) exit 0 ;;
esac
"""


def _start_script() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found, so the start command was not read from the Worker")
    code = f"import {{ START }} from {json.dumps(str(KIT / 'src' / 'edge.js'))}; process.stdout.write(START)"
    return subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True, check=True).stdout


def _bash5() -> str:
    """The image's bash is Debian's 5.x. macOS ships 3.2, which has no `wait -n PID...`."""
    for bash in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash")):
        if bash and Path(bash).exists():
            v = subprocess.run([bash, "-c", "echo ${BASH_VERSINFO[0]}${BASH_VERSINFO[1]}"],
                               capture_output=True, text=True, check=False).stdout.strip()
            if v and int(v[0]) >= 5:
                return bash
    pytest.skip("no bash 5 here, so the start command was not run (CI's ubuntu has one)")


def _run_start(tmp_path, api_life: float, worker_life: float, built: bool = False):
    import os
    import time
    bin_dir, data = tmp_path / "bin", tmp_path / "data"
    bin_dir.mkdir()
    data.mkdir()
    stub = bin_dir / "ironwright"
    stub.write_text(STUB)
    stub.chmod(0o755)
    if built:
        (data / ".demo-packs-built").touch()
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "IRONWRIGHT_DATA": str(data),
           "IW_LOG": str(log), "API_LIFE": str(api_life), "WORKER_LIFE": str(worker_life)}
    began = time.monotonic()
    done = subprocess.run([_bash5(), "-c", _start_script()], env=env, timeout=60, capture_output=True, text=True, check=False)
    return done, time.monotonic() - began, log.read_text().split(), data


def test_the_container_lives_past_the_demo_build_and_dies_with_the_api(tmp_path):
    done, took, calls, data = _run_start(tmp_path, api_life=3, worker_life=30)
    assert done.returncode == 1, done.stderr
    assert took >= 2.5, "the end of the demo build must not stop the container"
    assert took < 20, "the api died, so the container should have gone too"
    assert sorted(calls) == ["run", "serve-platform", "worker"]
    assert (data / ".demo-packs-built").exists()


def test_the_container_dies_with_the_worker_too(tmp_path):
    done, took, _, _ = _run_start(tmp_path, api_life=30, worker_life=2)
    assert done.returncode == 1 and 1.5 <= took < 20


def test_a_disk_with_the_packs_does_not_build_them_again(tmp_path):
    _, _, calls, _ = _run_start(tmp_path, api_life=1, worker_life=30, built=True)
    assert "run" not in calls
