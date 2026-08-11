"""§65 — "why blocked?" explainer tests.

The explainer must aggregate every *real* block signal (incident freeze,
degradations table, run.error tokens, per-step BLOCKED notes, fail-closed
unknown) into one answer and never invent reasons. A BLOCKED run with no
logged reason must still report *something* (unknown, critical) — silence is
itself a finding.
"""

import os
import tempfile

from sworker.store import WorkerStore
from sworker.block_explainer import BlockExplainer, explain_blocked, BlockReason
from sworker.incident import IncidentLedger
from sworker.safemode import SafeMode
from sworker.degradation import DegradationLedger, CRITICAL
from sworker.models import RunStatus, Run


def _store():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".state"), exist_ok=True)
    return WorkerStore(os.path.join(d, ".state"))


def _mk_run(store, status=RunStatus.BLOCKED, error="", degradations=None):
    run = Run(
        task_id="t", worker="w", id="run_x", status=status,
        error=error, degradations=degradations or [],
    )
    store.put("runs", run.to_dict(), event="run.created")
    return run


def test_missing_run_reports_unknown_not_false():
    store = _store()
    out = explain_blocked(store, "nope")
    assert out["was_blocked"] is None
    assert out["reasons"][0]["kind"] == "unknown"


def test_incident_freeze_surfaced():
    store = _store()
    _mk_run(store)
    IncidentLedger(store).open("breach", by="op")
    out = explain_blocked(store, "run_x")
    reasons = [r for r in out["reasons"] if r["kind"] == "incident_active"]
    assert reasons, "open incident must surface as a block reason"
    assert reasons[0]["severity"] == "critical"


def test_degradation_table_surfaced():
    store = _store()
    _mk_run(store)
    DegradationLedger(store, run_id="run_x").record(
        "safe_mode_block", "safe mode is locked", severity=CRITICAL,
        mitigation="sworker safemode off", run_id="run_x")
    out = explain_blocked(store, "run_x")
    reasons = [r for r in out["reasons"] if r["kind"] == "safe_mode_block"]
    assert reasons
    assert reasons[0]["mitigation"] == "sworker safemode off"


def test_run_error_tokens_mapped():
    store = _store()
    _mk_run(store, error="incident_active")
    out = explain_blocked(store, "run_x")
    assert any(r["kind"] == "incident_active" for r in out["reasons"])

    store2 = _store()
    _mk_run(store2, error="resource_exhausted")
    out2 = explain_blocked(store2, "run_x")
    assert any(r["kind"] == "resource_exhausted" for r in out2["reasons"])


def test_step_blocked_note_surfaced():
    store = _store()
    _mk_run(store)
    store.put("steps", {
        "id": "s1", "run_id": "run_x", "idx": 0, "plan_id": "p",
        "description": "write file", "tool": "fs.write", "status": "BLOCKED",
        "note": "action denied by permission policy: not allowed",
    }, event="step.blocked")
    out = explain_blocked(store, "run_x")
    reasons = [r for r in out["reasons"] if r["source"] == "steps"]
    assert reasons
    # permission_denied kind detected from the note text
    assert any(r["kind"] == "permission_denied" for r in reasons)


def test_blocked_with_no_reason_reports_unknown():
    store = _store()
    # BLOCKED but no degradation / no error / no blocked step
    _mk_run(store, error="")
    out = explain_blocked(store, "run_x")
    assert out["was_blocked"] is True
    assert any(r["kind"] == "unknown" and r["severity"] == "critical"
               for r in out["reasons"])


def test_not_blocked_reports_clean():
    store = _store()
    _mk_run(store, status=RunStatus.SUCCESS)
    out = explain_blocked(store, "run_x")
    assert out["was_blocked"] is False
    # a clean run may still surface degradations, but was_blocked is False


def test_workspace_explain_aggregates_incident():
    store = _store()
    IncidentLedger(store).open("w", by="op")
    out = BlockExplainer(store).explain_workspace()
    assert out["was_blocked"] is True
    assert any(r["kind"] == "incident_active" for r in out["reasons"])


def test_fail_closed_unknown_inputs():
    # missing record -> was_blocked None, never False
    store = _store()
    out = BlockExplainer(store).explain_run("ghost")
    assert out["was_blocked"] is None
    # invalid status string (stored as a raw value, bypassing the enum)
    d = _mk_run(store, status=RunStatus.BLOCKED).to_dict()
    d["status"] = "UNKNOWN_STATUS"
    store.put("runs", d, event="run.created")  # overwrite with bad status
    out2 = BlockExplainer(store).explain_run("run_x")
    assert out2["was_blocked"] is None
