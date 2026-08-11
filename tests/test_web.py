"""Integration tests for the local-first web UI (sworker.web).

Real HTTP against a live ThreadingHTTPServer, no mocks. Exercises the full
surface: index, submit-run, run page, approval -> resume loop, verify, and the
JSON API. Uses the same deterministic fallback engine as the CLI.

Auth model (post-rewrite): the UI requires a real session cookie issued by
/login. Tests log in as an operator (which holds the permissions every mutating
route needs) and carry that cookie. The fail-closed gating (no cookie -> 401,
cross-origin -> 403) is covered separately in tests/test_web_auth.py.

Run with:  env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/
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

from sworker.config import Workspace
from sworker.store import WorkerStore
from sworker.approvals import ApprovalManager
from sworker.auth import AuthProvider
from sworker import web as web_mod


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
instructions: |
  Compute figures from the CSVs with data.query.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: auto
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
"""

# A worker whose reversible writes require approval -> exercises the gate.
GATED_WORKER_YAML = """name: acme-gated
role: Acme Coffee gated analyst
instructions: |
  Compute figures from the CSVs with data.query.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: approve
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture 3xx responses instead of following them."""

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
    (home / "workers" / "acme-gated.yaml").write_text(GATED_WORKER_YAML)
    os.environ["SWORKER_HOME"] = str(home)
    w = Workspace(str(home))
    w.ensure()
    # seed an operator account for the UI login
    AuthProvider(WorkerStore(w.state_dir)).create_user("op", "oppass", role="operator")
    return w


def _start_server(ws):
    from http.server import ThreadingHTTPServer

    store = WorkerStore(ws.state_dir)
    from sworker.auth import AuthProvider
    from sworker.rbac import RBAC

    port = _free_port()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        lambda *a, **k: web_mod.Handler(
            *a, store=store, ws=ws, auth=AuthProvider(store), rbac=RBAC(), port=port, **k
        ),
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _login(port: int, username: str = "op", password: str = "oppass") -> str:
    """Log in and return the session cookie value."""
    from http.cookiejar import CookieJar

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), _NoRedirect())
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": f"http://127.0.0.1:{port}/"},
    )
    try:
        opener.open(req, timeout=3)  # 303 not followed; cookie captured in jar
    except HTTPError:
        pass  # _NoRedirect raises on 303; cookie is still in the jar
    for c in cj:
        if c.name == "sworker_session":
            return c.value
    raise RuntimeError("login did not yield a session cookie")


def _post(port: int, path: str, form: dict, cookie: str) -> HTTPResponse:
    url = f"http://127.0.0.1:{port}{path}"
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Cookie", f"sworker_session={cookie}")
    req.add_header("Origin", f"http://127.0.0.1:{port}/")
    try:
        return _opener.open(req)
    except HTTPError as e:  # 4xx/5xx still carry a response
        return e


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


def test_index_lists_workers_and_run_form(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _get(port, "/", cookie)
        assert resp.status == 200, resp.status
        html = _body(resp)
        assert "Sovereign AI Worker" in html
        assert "New run" in html
        assert "acme-analyst" in html
        assert "acme-gated" in html
        # JSON API works
        jr = _get(port, "/api/runs", cookie)
        assert jr.status == 200
        assert jr.headers["Content-Type"].startswith("application/json")
    finally:
        httpd.shutdown()


def test_submit_run_and_view_success(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _post(port, "/run", {"worker": "acme-analyst",
                                    "request": "What was total Q2 revenue?"}, cookie)
        assert resp.status == 303, resp.status
        loc = resp.headers["Location"]
        assert "run_id=" in loc
        run_id = loc.split("run_id=")[1]

        page = _get(port, f"/run?run_id={run_id}", cookie)
        assert page.status == 200
        html = _body(page)
        assert "SUCCESS" in html
        assert "188500" in html
        assert "Evidence" in html
        assert "Audit trail" in html
    finally:
        httpd.shutdown()


def test_invalid_submit_rejected(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _post(port, "/run", {"worker": "", "request": ""}, cookie)
        assert resp.status == 400
    finally:
        httpd.shutdown()


def test_verify_page_runs_derived_checks(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _post(port, "/run", {"worker": "acme-analyst",
                                    "request": "What was total Q2 revenue?"}, cookie)
        run_id = resp.headers["Location"].split("run_id=")[1]

        # Fallback runs now auto-derive recompute_sum checks; /verify should run
        # them and report ALL PASSED (the derived total re-matches the source).
        v = _get(port, f"/verify?run_id={run_id}", cookie)
        assert v.status == 200
        body = _body(v)
        assert "ALL PASSED" in body
        assert "recompute_sum" in body
        assert "no verification checks declared" not in body
    finally:
        httpd.shutdown()


def test_approval_resume_loop_with_session(ws):
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _post(port, "/run", {"worker": "acme-gated",
                                    "request": "What was total Q2 revenue?"}, cookie)
        assert resp.status == 303, resp.status
        run_id = resp.headers["Location"].split("run_id=")[1]

        store = WorkerStore(ws.state_dir)
        pending = ApprovalManager(store).pending(run_id)
        assert pending, "expected a pending approval"
        appr_id = pending[0]["id"]

        ar = _post(port, "/approve", {"appr_id": appr_id}, cookie)
        assert ar.status == 303, ar.status
        rr = _post(port, "/resume", {"run_id": run_id}, cookie)
        assert rr.status == 303, rr.status

        final = _get(port, f"/run?run_id={run_id}", cookie)
        assert final.status == 200
        assert "SUCCESS" in _body(final)
    finally:
        httpd.shutdown()


# --- §37 /api/v1 + §36 web hardening ----------------------------------------


def test_api_v1_unauthenticated_is_rejected(ws):
    """§36 — the versioned API must not leak without a session (fail-closed)."""
    httpd, port = _start_server(ws)
    try:
        resp = _get(port, "/api/v1/workers", cookie="")
        assert resp.status in (401, 403), resp.status
    finally:
        httpd.shutdown()


def test_api_v1_health_and_workers_and_metrics(ws):
    """§37 — versioned JSON endpoints return structured data + hardening headers."""
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        for path in ("/api/v1/health", "/api/v1/workers", "/api/v1/metrics"):
            resp = _get(port, path, cookie)
            assert resp.status == 200, (path, resp.status)
            # §36 hardening headers present on JSON responses
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")
            data = json.loads(_body(resp))
            if path == "/api/v1/workers":
                assert "acme-analyst" in data and "acme-gated" in data
            elif path == "/api/v1/metrics":
                assert isinstance(data, dict)
            else:  # health
                assert "ok" in [c.get("status") for c in data.get("checks", [])]
    finally:
        httpd.shutdown()


def test_api_v1_openapi_doc(ws):
    """§37 — self-describing OpenAPI doc is served and parseable."""
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        resp = _get(port, "/api/v1/openapi.json", cookie)
        assert resp.status == 200
        doc = json.loads(_body(resp))
        assert doc["openapi"].startswith("3.")
        assert "/api/v1" == doc["servers"][0]["url"]
        assert "/health" in doc["paths"] and "/runs" in doc["paths"]
        assert "sessionCookie" in doc["components"]["securitySchemes"]
    finally:
        httpd.shutdown()


def test_api_v1_run_detail_and_explain(ws):
    """§37 — run lookup returns 404 on miss; explain POST returns a plan (no run)."""
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        miss = _get(port, "/api/v1/runs/does-not-exist", cookie)
        assert miss.status == 404
        ex = _post(port, "/api/v1/explain",
                   {"worker": "acme-analyst", "request": "total Q2 revenue?"}, cookie)
        assert ex.status == 200, ex.status
        plan = json.loads(_body(ex))
        assert "intent" in plan and "steps" in plan
        # explain must never create a Run
        runs = _get(port, "/api/v1/runs", cookie)
        assert all(r.get("worker") != "acme-analyst" for r in json.loads(_body(runs)))
    finally:
        httpd.shutdown()


def test_dashboard_page_and_api(ws):
    """§27 — admin dashboard renders and the JSON summary endpoint works."""
    httpd, port = _start_server(ws)
    try:
        cookie = _login(port)
        page = _get(port, "/dashboard", cookie)
        assert page.status == 200, page.status
        body = _body(page)
        assert "Workers" in body and "Runs by status" in body
        # JSON summary endpoint (versioned)
        resp = _get(port, "/api/v1/dashboard", cookie)
        assert resp.status == 200, resp.status
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        body = _body(resp)
        data = json.loads(body)
        assert "runs_by_status" in data and "health" in data and "metrics" in data
        assert "acme-analyst" in data["workers"]
        # no secrets surfaced
        assert "xoxb" not in body.lower()
    finally:
        httpd.shutdown()
