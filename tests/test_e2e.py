"""§68 — end-to-end platform integration.

These are real integration tests that exercise the *whole* platform stack through
the public engine API: a deterministic (no-LLM) run that produces SUCCESS with an
independently re-derived verification chain; the audit hash-chain staying intact
end to end; the hardening controls (§62 safe mode, §63 incident freeze) actually
gating real runs; and §66 aggregating a real incident into a CRITICAL verdict.

No mocks, no cloud, no language model. Every assertion reads persisted state.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from sworker.config import Workspace, get_worker
from sworker.store import WorkerStore
from sworker.engine import WorkerEngine
from sworker.inference import NullInference
from sworker.tools import build_registry
from sworker.incident import IncidentLedger
from sworker.system_status import SystemStatus, CRITICAL
from sworker.models import RunStatus


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
  Compute figures from the CSVs with data.query; never state a number you did
  not derive. Write a markdown report that cites source totals.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: auto
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
"""


@pytest.fixture()
def ws(tmp_path):
    home = tmp_path / "acme"
    home.mkdir()
    (home / "company").mkdir()
    (home / "company" / "sales.csv").write_text(SALES_CSV)
    (home / "workers").mkdir()
    (home / "workers" / "acme-analyst.yaml").write_text(WORKER_YAML)
    os.environ["SWORKER_HOME"] = str(home)
    w = Workspace(str(home))
    w.ensure()
    return w


def make_engine(ws):
    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    return WorkerEngine(worker, store, inference=NullInference(), registry=build_registry())


def test_full_run_succeeds_with_derived_verification_chain(ws):
    """The product's core promise: a real run, re-derivable from source."""
    engine = make_engine(ws)
    result = engine.run("What was total Q2 revenue?")
    assert result.status == RunStatus.SUCCESS, result.summary
    # derived figure is stated and matches independent recompute
    assert "188,500" in result.summary or "188500" in result.summary, result.summary
    assert len(result.artifacts) >= 1
    # auto-derived verification re-sums the same source rows and passes
    vers = engine.store.find("verifications", run_id=result.run.id)
    assert vers, "expected auto-derived verification checks"
    assert all(v["outcome"] == "PASS" for v in vers), vers
    assert any(v["check"] == "recompute_sum" and v["expected"] == "188500.0" for v in vers)


def test_audit_chain_intact_after_run(ws):
    """Every mutating step is hash-chained and tamper-evident."""
    engine = make_engine(ws)
    engine.run("Q2 revenue total?")
    chain = engine.store.verify_audit_chain()
    assert chain["ok"], chain
    assert chain["checked"] > 0, chain


def test_incident_freezes_new_runs_fail_closed(ws):
    """§63: an open incident must refuse new runs, reported BLOCKED not dropped."""
    engine = make_engine(ws)
    # open a real incident via the same subsystem the engine reads
    IncidentLedger(engine.store).open("live breach", by="op")
    result = engine.run("What was total Q2 revenue?")
    assert result.status == RunStatus.BLOCKED, (result.status, result.summary)
    assert result.run.error == "incident_active"
    # audit chain still intact (the block itself is recorded)
    assert engine.store.verify_audit_chain()["ok"]


def test_safemode_locked_blocks_tool_actions(ws):
    """§62: locked safe mode must block execution, not silently proceed."""
    engine = make_engine(ws)
    from sworker.safemode import SafeMode

    SafeMode(engine.store).lock()
    result = engine.run("What was total Q2 revenue?")
    # fail-closed: the run must not reach SUCCESS under safe-mode lockdown
    assert result.status != RunStatus.SUCCESS, (result.status, result.summary)
    assert result.status == RunStatus.BLOCKED, (result.status, result.summary)


def test_system_status_aggregates_real_incident(ws):
    """§66: a real incident must surface as a CRITICAL platform verdict."""
    engine = make_engine(ws)
    IncidentLedger(engine.store).open("live breach", by="op")
    out = SystemStatus(engine.store).compose()
    assert out["verdict"] == CRITICAL
    names = {c["name"] for c in out["controls"]}
    assert "incident" in names
    inc = next(c for c in out["controls"] if c["name"] == "incident")
    assert inc["severity"] == CRITICAL


GATED_WORKER_YAML = """name: acme-gated
role: Acme Coffee business analyst
instructions: |
  Compute figures from the CSVs with data.query; never state a number you did
  not derive.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: approve
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
"""


def make_gated_engine(ws):
    (Path(ws.workers_dir) / "acme-gated.yaml").write_text(GATED_WORKER_YAML)
    worker = get_worker("acme-gated", ws)
    store = WorkerStore(ws.state_dir)
    return WorkerEngine(worker, store, inference=NullInference(), registry=build_registry())


def test_cancel_is_terminal_and_idempotent(ws):
    """§11: cancel moves an awaiting-approval run to CANCELLED and is idempotent."""
    engine = make_gated_engine(ws)
    result = engine.run("What was total Q2 revenue?")
    assert result.status == RunStatus.AWAITING_APPROVAL, (result.status, result.summary)
    rid = result.run.id
    cancelled = engine.cancel(rid, by="user", reason="stop")
    assert cancelled.status == RunStatus.CANCELLED
    # idempotent: second cancel returns the same terminal run, no error
    again = engine.cancel(rid, by="user", reason="stop")
    assert again.status == RunStatus.CANCELLED
    assert again.id == cancelled.id


def test_run_and_verify_roundtrip_reproducible(ws):
    """The run is reconstructable from the ledger (spec principle #4)."""
    engine = make_engine(ws)
    r1 = engine.run("Q2 revenue by channel?")
    assert r1.status == RunStatus.SUCCESS
    # re-read the persisted run, steps, evidence, and verifications
    rec = engine.store.get("runs", r1.run.id)
    assert rec["status"] == "SUCCESS"
    steps = engine.store.find("steps", run_id=r1.run.id)
    assert steps, "run must persist its step records"
    assert engine.store.find("evidence", run_id=r1.run.id), "run must mint evidence"
    # the derived total must be re-derivable (same expected value as before)
    vers = engine.store.find("verifications", run_id=r1.run.id)
    assert any(v["check"] == "recompute_sum" for v in vers)
