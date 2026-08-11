"""§38 — CLI `--json` coherence across read/inspection commands.

Every inspection command emits the same structured view whether or not --json is
passed: with --json it is machine-readable JSON; without it is human text. These
tests drive the real CLI entrypoint (`main`) against a live workspace.
"""

import json
import os

import pytest

import yaml

from sworker.cli import main
from sworker.config import Workspace, default_workspace, get_worker
from sworker.store import WorkerStore
from sworker.auth import AuthProvider


WORKER = {
    "name": "analyst", "role": "local business analyst",
    "policy": {"read": "auto", "reversible": "auto", "external": "approve",
               "financial": "approve", "destructive": "approve"},
    "goal": "analyze", "tools": ["data.query", "fs.list"],
}


@pytest.fixture()
def ws(tmp_path):
    os.environ["SWORKER_HOME"] = str(tmp_path)
    os.environ.pop("SWORKER_WORKERS_DIR", None)
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    ws = default_workspace()
    os.makedirs(ws.workers_dir, exist_ok=True)
    os.makedirs(ws.state_dir, exist_ok=True)
    with open(os.path.join(ws.workers_dir, "analyst.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(WORKER, sort_keys=False, stream=fh)
    yield ws
    os.environ.pop("SWORKER_HOME", None)


def _run(capsys, *argv):
    try:
        code = main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    out = capsys.readouterr().out
    return code, out


def test_workers_json_and_plain(ws, capsys):
    code, out = _run(capsys, "workers")
    assert code == 0
    assert "analyst" in out
    code, out = _run(capsys, "workers", "--json")
    assert code == 0
    data = json.loads(out)
    assert any(d["name"] == "analyst" for d in data)
    assert data[0]["policy"]["read"] == "auto"


def test_show_json_and_plain(ws, capsys):
    code, out = _run(capsys, "show", "analyst")
    assert code == 0 and "analyst" in out
    code, out = _run(capsys, "show", "analyst", "--json")
    data = json.loads(out)
    assert data["name"] == "analyst" and data["tools"] == ["data.query", "fs.list"]


def test_runs_json_empty(ws, capsys):
    code, out = _run(capsys, "runs", "--json")
    assert code == 0
    assert json.loads(out) == []


def test_run_info_json_missing(ws, capsys):
    code, out = _run(capsys, "run-info", "nope", "--json")
    assert code == 1
    assert json.loads(out)["error"]


def test_audit_json_empty(ws, capsys):
    code, out = _run(capsys, "audit", "whatever", "--json")
    assert code == 0
    assert json.loads(out) == []


def test_proc_json_empty(ws, capsys):
    code, out = _run(capsys, "proc", "--json")
    assert code == 0
    assert json.loads(out) == []


def test_sched_json_empty(ws, capsys):
    code, out = _run(capsys, "sched", "list", "--json")
    assert code == 0
    assert json.loads(out) == []


def test_verify_json_no_checks(ws, capsys):
    # no run exists -> exit 1 with structured error-ish output
    code, out = _run(capsys, "verify", "run_x", "--json")
    assert code == 1


def test_policy_json_empty(ws, capsys):
    code, out = _run(capsys, "policy", "current", "default", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["scope"] == "default"
    assert data["hash"] is None


def test_user_json_lists(ws, capsys):
    ap = AuthProvider(WorkerStore(ws.state_dir))
    ap.create_user("alice", "pw", role="operator")
    code, out = _run(capsys, "user", "list", "--json")
    assert code == 0
    data = json.loads(out)
    assert any(u["username"] == "alice" and u["role"] == "operator" for u in data)


def test_knowledge_status_json(ws, capsys):
    code, out = _run(capsys, "knowledge", "status", "--json")
    assert code == 0
    data = json.loads(out)
    assert "compiled" in data  # structured status dict


def test_connectors_json_default_deny(ws, capsys):
    code, out = _run(capsys, "connectors", "list", "analyst", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["worker"] == "analyst"
    assert data["connectors"] == {}


def test_browser_json(ws, capsys):
    code, out = _run(capsys, "browser", "policy", "analyst", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["worker"] == "analyst"
    assert "browser_allow" in data


def test_egress_json(ws, capsys):
    code, out = _run(capsys, "egress", "policy", "analyst", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["worker"] == "analyst"
    assert "egress_allow" in data


def test_dlp_json(ws, capsys):
    code, out = _run(capsys, "dlp", "policy", "analyst", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["worker"] == "analyst"
    assert "catalog" in data


def test_metrics_json(ws, capsys):
    code, out = _run(capsys, "metrics")
    assert code == 0
    data = json.loads(out)  # metrics already emits JSON
    assert isinstance(data, dict)


def test_onboard_creates_admin_then_is_idempotent(ws, capsys):
    """§46 — first onboard creates an admin user; a second run must not recreate users."""
    code, out = _run(capsys, "onboard", "--username", "admin", "--password", "s3cret")
    assert code == 0
    store = WorkerStore(ws.state_dir)
    users = store.find("users")
    assert len(users) == 1 and users[0]["username"] == "admin"
    # idempotent: a second onboard run leaves the existing user untouched
    code2, out2 = _run(capsys, "onboard", "--username", "admin", "--password", "s3cret")
    assert code2 == 0
    assert "already exist" in out2
    assert len(store.find("users")) == 1


def test_web_help_shows_host_flag(ws, capsys):
    """§47 — `web` exposes --host so the server can bind outside loopback in containers."""
    try:
        code = main(["web", "--help"])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code == 0
    assert "--host" in out


def test_phase5_docs_present():
    """§48/§49/§50 — professional docs set ships with the repo."""
    import sworker
    root = os.path.dirname(os.path.dirname(sworker.__file__))
    for rel in ("docs/OPERATIONS.md", "docs/DEMO.md", "docs/ARCHITECTURE.md",
                "docs/TRUST_BOUNDARY.md", "docs/THREAT_MODEL.md", "docs/GRACEFUL_DEGRADATION.md",
                "docs/SAFE_MODE.md", "docs/INCIDENT_RESPONSE.md", "docs/SECURITY_EVENTS.md", "docs/WHY_BLOCKED.md", "docs/SYSTEM_STATUS.md", "docs/INTEGRATION_TESTS.md", "docs/BENCHMARKS.md", "docs/MATURITY.md", "docs/PROCEDURES.md",
                "Dockerfile", "docker-compose.yml"):
        assert os.path.exists(os.path.join(root, rel)), rel
