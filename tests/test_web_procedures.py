"""§23 — web surface for the published-procedure review ledger.

Real HTTP against a live ThreadingHTTPServer (no mocks). Verifies the
GET /procedures HTML page and the GET /api/v1/procedures JSON mirror both
render the real, persisted published-procedure registry — and that neither
leaks procedure bodies, only the review metadata (name/version/pin/author/hash).
"""

from __future__ import annotations

import os
import socket
import threading
import json
import urllib.parse
import urllib.request
from http.client import HTTPResponse
from urllib.error import HTTPError

import pytest

from sworker.config import Workspace, get_worker
from sworker.store import WorkerStore
from sworker.auth import AuthProvider
from sworker import web as web_mod
from sworker import procedures as P


SALES_CSV = """region,quarter,revenue,orders
North,Q1,42000,1320
North,Q2,51000,1480
South,Q1,31000,980
South,Q2,35500,1100
Online,Q1,88000,4200
Online,Q2,102000,5100
"""

WORKER_YAML = """name: acme-analyst
role: Acme Coffee business analyst
instructions: Compute figures from the CSVs with data.query.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect]
fs_roots: [company]
"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def ws(tmp_path):
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "sales.csv").write_text(SALES_CSV)
    (home / "workers").mkdir(parents=True)
    (home / "workers" / "acme-analyst.yaml").write_text(WORKER_YAML)
    os.environ["SWORKER_HOME"] = str(home)
    w = Workspace(str(home))
    w.ensure()
    AuthProvider(WorkerStore(w.state_dir)).create_user("op", "oppass", role="operator")
    # seed a published procedure (bodies are never rendered by the ledger)
    worker = get_worker("acme-analyst", w)
    P.publish_procedure(worker, "weekly-report", "steps:\n  - compute totals\n")
    return w


def _start_server(ws):
    from http.server import ThreadingHTTPServer

    store = WorkerStore(ws.state_dir)
    port = _free_port()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        lambda *a, **k: web_mod.Handler(
            *a, store=store, ws=ws, auth=AuthProvider(store), rbac=__import__(
                "sworker.rbac", fromlist=["RBAC"]
            ).RBAC(), port=port, **k
        ),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _login(port: int, username: str = "op", password: str = "oppass") -> str:
    from http.cookiejar import CookieJar

    cj = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj), _NoRedirect()
    )
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": f"http://127.0.0.1:{port}/"},
    )
    try:
        opener.open(req, timeout=3)
    except HTTPError:
        pass
    for c in cj:
        if c.name == "sworker_session":
            return c.value
    raise RuntimeError("login did not yield a session cookie")


def _get(port: int, path: str, cookie: str = "") -> HTTPResponse:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if cookie:
        req.add_header("Cookie", f"sworker_session={cookie}")
    try:
        return _opener.open(req)
    except HTTPError as e:
        return e


def _body(resp) -> str:
    return resp.read().decode("utf-8", "replace")


def test_procedures_html_page_lists_published(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _get(port, "/procedures", cookie)
        assert resp.status == 200, resp.status
        html = _body(resp)
        assert "Published procedures" in html
        assert "acme-analyst" in html
        assert "weekly-report" in html
        assert "v1.0" in html
        # bodies never inlined on the review ledger
        assert "compute totals" not in html
    finally:
        httpd.shutdown()


def test_procedures_api_returns_published_metadata(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _get(port, "/api/v1/procedures", cookie)
        assert resp.status == 200, resp.status
        assert resp.headers["Content-Type"].startswith("application/json")
        payload = json.loads(_body(resp))
        procs = payload["procedures"]
        assert any(
            p["worker"] == "acme-analyst"
            and p["name"] == "weekly-report"
            and p["version"] == "1.0"
            and p["current"] is True
            and p["hash"]
            for p in procs
        )
        # no procedure body leaks through the API surface
        assert "compute totals" not in json.dumps(payload)
    finally:
        httpd.shutdown()
