"""The wizard in a real browser, under coconutlabs.org's path, through a proxy that acts like
the Vercel rewrite in front of the Worker.

The proxy does what the rewrite does, and nothing kind: it answers only under the prefix,
hands the request on with the upstream's Host (so the app never sees the browser's), and
strips a trailing slash with a 308 the way Next.js does before it rewrites. The browser's
Origin is the proxy's, which is why the server is told IRONWRIGHT_CONSOLE_ORIGIN, as the
Worker's config tells it in production.

The server is `ironwright serve-platform`, the hosted entry point the container runs, with a
worker beside it. No route stub anywhere. The walk itself is the one in
test_wizard_real_backend.py, so the two can never drift apart.
"""
from __future__ import annotations

import http.client
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from test_wizard_real_backend import (  # noqa: F401  fixtures are found by name
    CLEAN_CSVS,
    Walk,
    _free_port,
    _stack_env,
    _wait_until_up,
    browser,
    console_dist,
)

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/projects/ironwright/app"
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection", "host",
       "content-length"}


def rewrite_proxy(upstream_port: int) -> ThreadingHTTPServer:
    class Rewrite(BaseHTTPRequestHandler):
        def _any(self) -> None:
            path = self.path
            if not (path.split("?")[0] == PREFIX or path.startswith(PREFIX + "/")):
                return self._send(404, [], b"outside the rewrite")
            bare = path.split("?")[0]
            if bare.endswith("/") and bare != "/":
                query = path[len(bare):]
                return self._send(308, [("Location", bare.rstrip("/") + query)], b"")
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=120)
            headers["Host"] = f"127.0.0.1:{upstream_port}"
            conn.request(self.command, path, body=body or None, headers=headers)
            r = conn.getresponse()
            data = r.read()
            self._send(r.status, [(k, v) for k, v in r.getheaders() if k.lower() not in HOP], data)
            conn.close()

        def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _any

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Rewrite)


@pytest.fixture(scope="module")
def under_prefix(request, tmp_path_factory) -> Iterator[str]:
    request.getfixturevalue("console_dist")
    root = tmp_path_factory.mktemp("wizard-prefix")
    port = _free_port()
    proxy = rewrite_proxy(port)
    front = f"http://127.0.0.1:{proxy.server_address[1]}"
    env = {**_stack_env(root), "IRONWRIGHT_BASE_PATH": PREFIX, "IRONWRIGHT_PORT": str(port),
           "IRONWRIGHT_CONSOLE_ORIGIN": front, "IRONWRIGHT_RUNS_WAIT": "0"}
    env.pop("IRONWRIGHT_HOST", None)
    cli = [sys.executable, "-m", "ironwright.cli"]
    server_log, worker_log = root / "platform.log", root / "worker.log"
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    with server_log.open("w") as out, worker_log.open("w") as work:
        server = subprocess.Popen([*cli, "serve-platform"], cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT)
        worker = subprocess.Popen([*cli, "worker"], cwd=root, env=env, stdout=work, stderr=subprocess.STDOUT)
        try:
            _wait_until_up(f"http://127.0.0.1:{port}", server, server_log)
            yield front + PREFIX
        finally:
            proxy.shutdown()
            for process in (worker, server):
                process.terminate()
            for process in (worker, server):
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()


def test_the_proxy_is_as_strict_as_the_rewrite(under_prefix):
    """A stray absolute URL in the console would 404 here, as it would on coconutlabs.org."""
    front = under_prefix.removesuffix(PREFIX)
    host, port = front.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("GET", "/auth/status")
    assert conn.getresponse().status == 404
    conn.close()
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("GET", f"{PREFIX}/console/")
    r = conn.getresponse()
    assert (r.status, r.getheader("location")) == (308, f"{PREFIX}/console")


def test_sign_up_to_a_cited_answer_under_the_prefix(browser, under_prefix):  # noqa: F811
    walk = Walk(browser, under_prefix, "prefix")
    walk.page.goto(under_prefix)  # the "Try it" link: the bare prefix, no slash
    assert walk.page.url == f"{under_prefix}/console", walk.page.url
    walk.to_map(CLEAN_CSVS)
    proposal = walk.to_proposal("acme-prefix")
    assert proposal["objects"], proposal
    walk.accept_and_ask()
    walk.assert_nothing_was_swallowed()
    walk.context.close()


def test_ci_runs_this_file_and_a_skip_fails_the_job():
    demo = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["demo"]
    commands = "\n".join(step.get("run", "") for step in demo["steps"])
    assert Path(__file__).name in commands and "! grep -q SKIPPED" in commands
