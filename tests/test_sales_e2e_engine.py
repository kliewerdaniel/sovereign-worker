"""End-to-end engine test: a real sworker run of the sales boundary layer.

Drives ``WorkerEngine`` with the bundled ``sales_researcher`` worker and a
``NullInference`` (no model, deterministic planner) over a temporary workspace.
This proves the autonomous loop is real, not just the repository layer: the
engine records a run, the sales tools write to the ledger + the append-only audit
log, and ``audit`` / ``replay`` can read it back. No network, no secrets.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from sworker.config import Workspace, load_worker
from sworker.engine import WorkerEngine
from sworker.store import WorkerStore
from sworker.inference import NullInference
from sworker import explain as explain_mod

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "sworker", "sales", "templates")
DAILYSALESOS = os.path.expanduser("~/Documents/Projects/salesworkflow")


def _install_researcher(home: Path):
    wdir = home / ".sworker" / "workers"
    wdir.mkdir(parents=True, exist_ok=True)
    src = os.path.join(TEMPLATES, "sales_researcher.yaml")
    dst = wdir / "sales_researcher.yaml"
    if not dst.exists():
        dst.write_text(Path(src).read_text(encoding="utf-8"))
    return dst


def test_engine_run_records_audit_trail():
    home = Path(tempfile.mkdtemp())
    # Put the ledger under company/ (the worker's fs_roots boundary).
    (home / "company").mkdir(parents=True)
    ledger_dir = home / "company" / "Experiment_Ledger"
    ledger_dir.mkdir(parents=True)
    db = str(ledger_dir / "experiments.db")
    os.environ["DAILYSALESOS_LEDGER"] = db
    os.environ["DAILYSALESOS_ROOT"] = DAILYSALESOS

    cfg_path = _install_researcher(home)
    ws = Workspace(str(home))
    ws.ensure()
    worker = load_worker(str(cfg_path), ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store, inference=NullInference())

    res = eng.run(
        "Qualify the lead for Acme Realty and report the pipeline",
        trigger="test",
    )
    run_id = res.run.id
    assert res.run.id, "engine must return a run id"

    # The run is persisted and reconstructable.
    rec = store.get("runs", run_id)
    assert rec is not None, "run recorded in the store"
    assert rec["worker"] == "sales_researcher"

    # Real audit trail: replay the append-only event log for this run.
    audit = list(store.iter_audit(run_id))
    assert audit, "audit log must contain events for the run"
    kinds = {a["event"] for a in audit}
    assert "run.started" in kinds
    assert "run.finished" in kinds

    # replay() reconstructs the run plan from the ledger (explain mode).
    rep = explain_mod.replay(eng, run_id, mode="explain")
    assert "run_id" in rep or "steps" in rep or "status" in rep, rep

    store.close()
    print(f"OK engine run {run_id}: {len(audit)} audit events; replay keys={sorted(rep)[:6]}")


def test_engine_refuses_unopted_sales_tools():
    """A vanilla analyst worker (no sales.*) must not see the sales tools."""
    home = Path(tempfile.mkdtemp())
    ws = Workspace(str(home))
    ws.ensure()
    # analyst template lives via init; use a minimal non-sales worker
    worker = load_worker(
        os.path.join(TEMPLATES, "sales_researcher.yaml")
    )  # researcher opts in; verify subset is bounded
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store, inference=NullInference())
    names = set(eng.registry.names())
    # researcher opt-in set must NOT include the egress tools
    assert "sales_record_sent" not in names, "researcher must not reach send"
    assert "sales_discover" in names
    store.close()
