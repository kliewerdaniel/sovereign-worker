"""Cancellation (spec §11).

Cancellation is meaningful for a NON-terminal run (the engine runs synchronously,
so a run that has already finished is terminal). The realistic, testable case is
a run sitting in AWAITING_APPROVAL: cancel moves it to the CANCELLED terminal
state, records who/when/why, and is idempotent on already-terminal runs.

The subprocess-kill path is exercised by registering a live child pid on the
engine's in-flight context and confirming cancel terminates its whole process
group.
"""

from __future__ import annotations

import os
import threading
import time

from sworker.config import Workspace, get_worker
from sworker.engine import WorkerEngine
from sworker.store import WorkerStore
from sworker.models import RunStatus
from sworker.tools.base import ToolContext
from sworker.tools.exec import run_in_sandbox


def _make_engine(tmp_path, worker_yaml, name):
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "sales.csv").write_text(
        "region,quarter,revenue,orders\nNorth,Q1,42000,1320\nNorth,Q2,51000,1480\n"
        "South,Q1,31000,980\nSouth,Q2,35500,1100\nOnline,Q1,88000,4200\nOnline,Q2,102000,5100\n"
    )
    (home / "workers").mkdir()
    (home / "workers" / f"{name}.yaml").write_text(worker_yaml)
    ws = Workspace(str(home))
    w = get_worker(name, ws)
    return WorkerEngine(w, WorkerStore(ws.state_dir))


# A worker whose reversible writes require approval -> run ends AWAITING_APPROVAL.
GATED_WORKER = """name: t
role: t
instructions: compute figures.
tools: [fs.list, fs.read, data.query, fs.write, knowledge.search]
fs_roots: [company]
policy:
  read: auto
  reversible: approve
  external: approve
  financial: approve
  destructive: approve
"""


def test_cancel_awaiting_approval_to_cancelled(tmp_path):
    eng = _make_engine(tmp_path, GATED_WORKER, "t")
    res = eng.run("What was total Q2 revenue?")
    assert res.status == RunStatus.AWAITING_APPROVAL
    cancelled = eng.cancel(res.run.id, by="alice", reason="no longer needed")
    assert cancelled.status == RunStatus.CANCELLED
    persisted = eng.store.get("runs", res.run.id)
    assert persisted["status"] == RunStatus.CANCELLED.value
    assert persisted["error"] == "no longer needed"


def test_cancel_records_who_and_why(tmp_path):
    eng = _make_engine(tmp_path, GATED_WORKER, "t")
    res = eng.run("What was total Q2 revenue?")
    eng.cancel(res.run.id, by="bob", reason="duplicate request")
    events = [
        r for r in eng.store.iter_audit(res.run.id) if r["event"] == "run.transition"
    ]
    t = [e for e in events if e["payload"]["to_state"] == RunStatus.CANCELLED.value]
    assert t, "expected a CANCELLED transition event"
    assert t[0]["payload"]["actor"] == "bob"
    assert t[0]["payload"]["reason"] == "duplicate request"


def test_cancel_idempotent_on_terminal(tmp_path):
    eng = _make_engine(tmp_path, GATED_WORKER, "t")
    res = eng.run("What was total Q2 revenue?")
    eng.cancel(res.run.id, by="x", reason="r")
    again = eng.cancel(res.run.id, by="x", reason="r")
    assert again.status == RunStatus.CANCELLED  # no error, no double transition


def test_cancel_kills_live_subprocess(tmp_path):
    eng = _make_engine(tmp_path, GATED_WORKER, "t")
    res = eng.run("What was total Q2 revenue?")
    assert res.status == RunStatus.AWAITING_APPROVAL

    # simulate an in-flight subprocess tracked on the engine's active context
    ctx = ToolContext(
        worker=eng.worker.name,
        run_id=res.run.id,
        workspace=eng.worker.workspace,
        fs_roots=eng.worker.resolved_fs_roots(),
        artifacts_dir=eng.worker.artifacts_dir(),
        shell_allow=["sleep"],
        env_allow=[],
    )
    eng._active_ctx = ctx

    def go():
        run_in_sandbox(["sleep", "30"], ctx, timeout=120)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(0.3)
    assert ctx.running_subprocesses, "expected a live subprocess pid registered"
    pid = next(iter(ctx.running_subprocesses))

    eng.cancel(res.run.id, by="ops", reason="kill it")
    time.sleep(0.5)

    try:
        os.kill(pid, 0)
        still_alive = True
    except OSError:
        still_alive = False
    assert not still_alive, "subprocess should have been killed by cancel"
    assert eng.store.get("runs", res.run.id)["status"] == RunStatus.CANCELLED.value
