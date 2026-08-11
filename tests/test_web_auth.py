"""End-to-end web auth + RBAC gating (real HTTP server, no live sockets to the user)."""

import os
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from http.cookiejar import CookieJar

import pytest

from sworker.store import WorkerStore
from sworker.auth import AuthProvider
from sworker.web import serve


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="sworker-web-")
    os.environ["SWORKER_HOME"] = d
    os.environ.pop("SWORKER_USER", None)
    # seed a minimal 'analyst' worker so the engine can resolve it
    ws_root = d
    os.makedirs(os.path.join(ws_root, "workers"), exist_ok=True)
    os.makedirs(os.path.join(ws_root, "company"), exist_ok=True)
    with open(os.path.join(ws_root, "workers", "analyst.yaml"), "w") as f:
        f.write(
            "name: analyst\nrole: local business analyst\ninstructions: |\n"
            "  Read company data under company/, answer questions with computed evidence.\n"
            "tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]\n"
        )
    with open(os.path.join(ws_root, "company", "example.csv"), "w") as f:
        f.write("region,quarter,revenue\nnorth,Q1,120\nnorth,Q2,150\nsouth,Q1,90\nsouth,Q2,140\n")
    return d


def _start_server(home, port):
    srv = threading.Thread(target=serve, kwargs={"port": port, "home": home}, daemon=True)
    srv.start()
    # wait for bind (any HTTP response counts, including 401 auth redirects)
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=0.3) as r:
                return srv
        except urllib.error.HTTPError:
            return srv
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _create_user(home, name, password, role):
    store = WorkerStore(os.path.join(home, ".state"))
    ap = AuthProvider(store)
    ap.create_user(name, password, role=role)


def _new_session():
    """A cookie-jar opener that persists the session cookie across redirects."""
    cj = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _get(opener, port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with opener.open(req, timeout=2) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post(opener, port, path, data):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{port}/"},
    )
    try:
        with opener.open(req, timeout=2) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def test_web_requires_auth(home):
    port = 8790
    _start_server(home, port)
    op = _new_session()
    # no cookie -> 401
    code, _ = _get(op, port, "/")
    assert code == 401
    # login page reachable
    code, body = _get(op, port, "/login")
    assert code == 200 and "Sign in" in body


def test_web_login_and_rbac_run_gating(home):
    port = 8791
    _start_server(home, port)
    _create_user(home, "op", "oppass", role="operator")
    _create_user(home, "viewer", "vpass", role="viewer")

    # wrong password -> 401
    op = _new_session()
    code, _, _ = _post(op, port, "/login", {"username": "op", "password": "WRONG"})
    assert code == 401

    # operator login -> session cookie persists; following 303 to / yields 200
    op = _new_session()
    code, hdr, body = _post(op, port, "/login", {"username": "op", "password": "oppass"})
    assert code in (303, 200)
    # authenticated GET / works (cookie carried through any redirect)
    code, body = _get(op, port, "/")
    assert code == 200 and "Workers" in body

    # viewer session
    vp = _new_session()
    _post(vp, port, "/login", {"username": "viewer", "password": "vpass"})
    code, _ = _get(vp, port, "/")
    assert code == 200  # viewers can still read

    # viewer POST /run -> 403 (no run:create)
    code, _, _ = _post(vp, port, "/run", {"worker": "analyst", "request": "hi"})
    assert code == 403

    # operator POST /run -> 303 (run created), cookie carried; jar follows to run view (200)
    code, _, _ = _post(op, port, "/run", {"worker": "analyst", "request": "summarize sales"})
    assert code in (303, 200)


def test_web_logout_revokes_session(home):
    port = 8792
    _start_server(home, port)
    _create_user(home, "op", "oppass", role="operator")

    op = _new_session()
    _post(op, port, "/login", {"username": "op", "password": "oppass"})
    code, _ = _get(op, port, "/")
    assert code == 200

    # logout clears the cookie server-side
    _post(op, port, "/logout", {})
    code, _ = _get(op, port, "/")
    assert code == 401
