"""§28 explainability / §29 dry-run / §30 replay distinction.

Fail-closed guarantees:
  * explain never executes a tool and never writes a Run to the store.
  * explain reports denied/awaited dispositions using the real PermissionEngine.
  * replay explain reads the ledger (no model); replay rerun calls engine.run.
  * disabled worker refused by explain (same boundary as run()).
"""

import os

import pytest

from sworker.config import Workspace, default_workspace, get_worker, load_worker
from sworker.engine import WorkerEngine, WorkerStore  # type: ignore
from sworker.explain import explain, replay
from sworker.logging import StructuredLogger, log_event, redact


@pytest.fixture
def eng(tmp_path):
    os.environ["SWORKER_HOME"] = str(tmp_path)
    os.environ.pop("SWORKER_WORKERS_DIR", None)
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    ws = default_workspace()
    os.makedirs(ws.workers_dir, exist_ok=True)
    os.makedirs(ws.state_dir, exist_ok=True)
    # analyst worker with deterministic tools only
    data = {
        "name": "explainer", "role": "analyst",
        "policy": {"read": "auto", "reversible": "auto", "external": "approve",
                   "financial": "approve", "destructive": "approve"},
        "goal": "explain", "tools": ["data.query", "fs.list"],
    }
    p = os.path.join(ws.workers_dir, "explainer.yaml")
    import yaml
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, sort_keys=False, stream=fh)
    w = get_worker("explainer", ws)
    e = WorkerEngine(w, WorkerStore(ws.state_dir))
    os.makedirs(os.path.join(ws.root, "company"), exist_ok=True)
    yield e
    os.environ.pop("SWORKER_HOME", None)


def test_explain_plan_only_no_run_written(eng):
    before = len(list(eng.store.iter_audit()))
    res = explain(eng, "summarize Q2 revenue")
    after = len(list(eng.store.iter_audit()))
    assert after == before, "explain must not write to the audit ledger"
    assert res.intent
    assert res.steps  # plan produced step explanations


def test_explain_disabled_worker_refused(tmp_path):
    os.environ["SWORKER_HOME"] = str(tmp_path)
    os.environ.pop("SWORKER_WORKERS_DIR", None)
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    ws = default_workspace()
    os.makedirs(ws.workers_dir, exist_ok=True)
    os.makedirs(ws.state_dir, exist_ok=True)
    import yaml
    data = {"name": "off", "role": "analyst", "disabled": True,
            "policy": {"read": "auto", "reversible": "auto", "external": "approve",
                       "financial": "approve", "destructive": "approve"},
            "goal": "g", "tools": ["data.query"]}
    with open(os.path.join(ws.workers_dir, "off.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, sort_keys=False, stream=fh)
    w = get_worker("off", ws)
    e = WorkerEngine(w, WorkerStore(ws.state_dir))
    with pytest.raises(RuntimeError):
        explain(e, "x")
    os.environ.pop("SWORKER_HOME", None)


def test_replay_explain_reads_ledger(eng):
    # run a real (deterministic) run, then explain-replay it from the ledger
    r = eng.run("list company data files")
    rep = replay(eng, r.run.id, mode="explain")
    assert rep["mode"] == "explain"
    assert rep["event_count"] > 0
    assert rep["actions"]  # reconstructed actions from ledger
    # rerun path actually executes
    rep2 = replay(eng, r.run.id, mode="rerun")
    assert rep2["mode"] == "rerun"
    assert rep2["run_id"] != r.run.id


def test_redaction_masks_secrets_by_default():
    line = log_event("cred", {"token": "abc123", "user": "bob@x.com", "note": "ok"})
    assert "abc123" not in line
    assert "bob@x.com" not in line  # email masked
    assert "ok" in line
    # opt-out is explicit
    raw = log_event("cred", {"token": "abc123"}, redact=False)
    assert "abc123" in raw


def test_structured_logger_captures():
    import io
    sink = io.StringIO()
    log = StructuredLogger(sink=sink, redact=False)
    log.info("tool.ran", tool="fs.list", n=3)
    out = sink.getvalue().strip()
    assert '"event": "tool.ran"' in out
    assert '"n": 3' in out
