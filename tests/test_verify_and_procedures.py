"""Unit tests for deterministic verification, scheduler, and procedural memory."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sworker.config import Workspace
from sworker.store import WorkerStore
from sworker.verify import run_check, available_checks, VerificationOutcome
from sworker.scheduler import parse_cron, next_fire
from sworker.procedures import (
    learn_from_run,
    save_procedure,
    list_procedures,
    load_procedure,
    substitute,
    procedure_steps,
)
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
    return w


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_available_checks_nonempty():
    checks = available_checks()
    assert "recompute_sum" in checks
    assert "recompute_delta_pct" in checks
    assert "row_count" in checks


def test_recompute_sum_pass(ws):
    spec = {
        "check": "recompute_sum",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "where": {"quarter": "Q2"},
        "expect": 188500.0,
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail
    assert res.actual == 188500.0


def test_recompute_sum_fail_on_wrong_expectation(ws):
    spec = {
        "check": "recompute_sum",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "where": {"quarter": "Q2"},
        "expect": 1.0,
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


def test_recompute_sum_unverifiable_without_expect(ws):
    spec = {
        "check": "recompute_sum",
        "path": "company/sales.csv",
        "value_column": "revenue",
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.UNVERIFIABLE
    assert res.actual is not None


def test_unknown_check_unverifiable(ws):
    res = run_check({"check": "nope", "path": "x"}, str(ws))
    assert res.status is VerificationOutcome.UNVERIFIABLE


def test_recompute_delta_pct(ws):
    spec = {
        "check": "recompute_delta_pct",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "current": {"quarter": "Q2"},
        "previous": {"quarter": "Q1"},
        "expect": 16.977,  # (188500-161000)/161000*100
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_path_escaping_blocked(ws):
    spec = {"check": "recompute_sum", "path": "../etc/passwd", "value_column": "x"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.UNVERIFIABLE


# --- §7 verification hardening ------------------------------------------------

def test_provenance_chain_pass(ws):
    # write a report artifact that cites the derived figure
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("# Q2 revenue\n\nQ2 revenue was 188,500.00 across regions.\n")
    spec = {
        "check": "provenance_chain",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "where": {"quarter": "Q2"},
        "expect": 188500.0,
        "artifact": "company/report.md",
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_provenance_chain_fails_when_artifact_does_not_cite(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("# Q2 revenue\n\nThe numbers are fine (trust me).\n")
    spec = {
        "check": "provenance_chain",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "where": {"quarter": "Q2"},
        "expect": 188500.0,
        "artifact": "company/report.md",
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail
    assert "DOES NOT cite" in res.detail


def test_provenance_chain_fails_when_source_mismatch(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("Q2 revenue was 999.00.\n")
    spec = {
        "check": "provenance_chain",
        "path": "company/sales.csv",
        "value_column": "revenue",
        "where": {"quarter": "Q2"},
        "expect": 188500.0,
        "artifact": "company/report.md",
    }
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


def test_finalize_fails_closed_on_unverifiable(ws):
    """§7: a run that produced an UNVERIFIABLE verification must not be SUCCESS."""
    from sworker.config import get_worker
    from sworker.engine import WorkerEngine
    from sworker.evidence import EvidenceLedger
    from sworker.models import Run, Verification, Evidence, Provenance, now

    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store)
    run = Run(worker=worker.name, task_id="t1", verifications=[{"check": "nope"}])
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    # evidence present so it would otherwise be SUCCESS
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    # an UNVERIFIABLE verification (unknown check) recorded for the run
    v = Verification(run_id=run.id, claim_id="", check="nope", outcome=VerificationOutcome.UNVERIFIABLE,
                    detail="unknown check")
    store.put("verifications", v, event="verification.recorded")
    ledger = EvidenceLedger(store, run.id)
    res = eng._finalize(run, ledger, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    assert res.status is RunStatus.PARTIAL_SUCCESS, res.summary
    assert "could not be proven" in (run.error or "")


def test_finalize_success_when_all_verifications_pass(ws):
    from sworker.config import get_worker
    from sworker.engine import WorkerEngine
    from sworker.evidence import EvidenceLedger
    from sworker.models import Run, Verification, Evidence, Provenance

    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store)
    run = Run(worker=worker.name, task_id="t2", verifications=[{"check": "recompute_sum"}])
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    v = Verification(run_id=run.id, claim_id="", check="recompute_sum", outcome=VerificationOutcome.PASS,
                    detail="ok")
    store.put("verifications", v, event="verification.recorded")
    ledger = EvidenceLedger(store, run.id)
    res = eng._finalize(run, ledger, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    assert res.status is RunStatus.SUCCESS, res.summary


# --- §15 generalized verification framework -----------------------------------


def test_schema_pass(ws):
    spec = {"check": "schema", "path": "company/sales.csv",
            "required_columns": ["region", "quarter", "revenue", "orders"]}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_schema_fail_missing_column(ws):
    spec = {"check": "schema", "path": "company/sales.csv",
            "required_columns": ["region", "quarter", "revenue", "orders", "missing_col"]}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail
    assert "missing_col" in res.detail


def test_schema_fail_type_violation(ws):
    # write a CSV where 'orders' is non-numeric in one row
    bad = ws.root + "/company/bad.csv"
    with open(bad, "w") as f:
        f.write("region,quarter,revenue,orders\nNorth,Q2,51000,not_a_number\n")
    spec = {"check": "schema", "path": "company/bad.csv",
            "required_columns": ["region", "orders"], "column_types": {"orders": "int"}}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


def test_schema_unverifiable_missing_source(ws):
    spec = {"check": "schema", "path": "company/nope.csv",
            "required_columns": ["x"]}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.UNVERIFIABLE


def test_set_equality_pass(ws):
    spec = {"check": "set_equality", "path": "company/sales.csv", "value_column": "region",
            "expected": ["North", "Online", "South"]}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail
    assert res.actual == ["North", "Online", "South"]


def test_set_equality_fail_extra_region(ws):
    spec = {"check": "set_equality", "path": "company/sales.csv", "value_column": "region",
            "expected": ["North", "South"]}  # missing Online
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


def test_set_equality_unverifiable_without_expected(ws):
    spec = {"check": "set_equality", "path": "company/sales.csv", "value_column": "region"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.UNVERIFIABLE


def test_regex_present_pass(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("Order ORD-2026-0042 shipped.\n")
    spec = {"check": "regex", "path": "company/report.md", "pattern": r"ORD-\d{4}-\d{4}"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_regex_absent_fail(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("No order id here.\n")
    spec = {"check": "regex", "path": "company/report.md", "pattern": r"ORD-\d{4}-\d{4}"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


def test_regex_want_absence_pass(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("Clean file.\n")
    spec = {"check": "regex", "path": "company/report.md",
            "pattern": r"SECRET", "present": False}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_doc_section_contains_pass(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("# Methodology\n\nFigures were summed from the CSV.\n\n# Notes\n\nEnd.\n")
    spec = {"check": "doc_section", "path": "company/report.md",
            "heading": "# Methodology", "contains": "summed from the CSV"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.PASS, res.detail


def test_doc_section_contains_fail(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("# Methodology\n\nFigures were guessed.\n")
    spec = {"check": "doc_section", "path": "company/report.md",
            "heading": "# Methodology", "contains": "summed from the CSV"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail
    assert "DOES NOT contain" in res.detail


def test_doc_section_missing_heading_fail(ws):
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write("# Other\n\nx\n")
    spec = {"check": "doc_section", "path": "company/report.md",
            "heading": "# Methodology", "contains": "x"}
    res = run_check(spec, ws.root)
    assert res.status is VerificationOutcome.FAIL, res.detail


# --- §16 claim-level provenance + artifact claim exposure ---------------------


def test_finalize_partial_success_when_artifact_surfaces_unbacked_claim(ws):
    """§16: an artifact that states a claim with NO provenance cannot be SUCCESS."""
    from sworker.config import get_worker
    from sworker.engine import WorkerEngine
    from sworker.evidence import EvidenceLedger
    from sworker.models import Run, Evidence, Provenance, Claim

    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store)
    run = Run(worker=worker.name, task_id="t16a")
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    # evidence exists so it would otherwise be SUCCESS
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    # a claim with no evidence/verification link
    c = Claim(run_id=run.id, text="Q2 revenue was 188,500.00", provenance=Provenance.HYPOTHESIZED)
    store.put("claims", c, event="claim.recorded")
    # artifact surfaces the claim text
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write(f"# Report\n\n{c.text}\n")
    a = {
        "id": "art_test16", "run_id": run.id, "path": art, "kind": "markdown",
        "bytes": os.path.getsize(art), "claim_ids": [c.id],
    }
    store.put("artifacts", a, event="artifact.created")
    ledger = EvidenceLedger(store, run.id)
    res = eng._finalize(run, ledger, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    assert res.status is RunStatus.PARTIAL_SUCCESS, res.summary
    assert "no provenance" in (run.error or "")


def test_finalize_success_when_surfaced_claim_is_backed(ws):
    """§16: a surfaced claim linked to evidence clears the bar -> SUCCESS."""
    from sworker.config import get_worker
    from sworker.engine import WorkerEngine
    from sworker.evidence import EvidenceLedger
    from sworker.models import Run, Evidence, Provenance, Claim

    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store)
    run = Run(worker=worker.name, task_id="t16b")
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    c = Claim(run_id=run.id, text="Q2 revenue was 188,500.00", provenance=Provenance.OBSERVED,
              evidence_ids=[ev.id])
    store.put("claims", c, event="claim.recorded")
    art = os.path.join(ws.root, "company", "report.md")
    with open(art, "w") as f:
        f.write(f"# Report\n\n{c.text}\n")
    a = {
        "id": "art_test16b", "run_id": run.id, "path": art, "kind": "markdown",
        "bytes": os.path.getsize(art), "claim_ids": [c.id],
    }
    store.put("artifacts", a, event="artifact.created")
    ledger = EvidenceLedger(store, run.id)
    res = eng._finalize(run, ledger, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    assert res.status is RunStatus.SUCCESS, res.summary
# scheduler
# ---------------------------------------------------------------------------


def test_parse_cron_alias():
    parsed = parse_cron("@daily")
    assert parsed["minute"] == [0]
    assert parsed["hour"] == [0]


def test_next_fire_daily():
    after = time.mktime(time.strptime("2026-01-01 12:00:00", "%Y-%m-%d %H:%M:%S"))
    nxt = next_fire("@daily", after=after)
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) == "2026-01-02 00:00"


def test_next_fire_weekdays():
    # Friday 2026-01-02 09:30 -> next weekday (Mon) 09:00
    after = time.mktime(time.strptime("2026-01-02 09:30:00", "%Y-%m-%d %H:%M:%S"))
    nxt = next_fire("0 9 * * 1-5", after=after)
    s = time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt))
    assert s == "2026-01-05 09:00", s


def test_next_fire_every_15_min():
    after = time.mktime(time.strptime("2026-01-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
    nxt = next_fire("*/15 * * * *", after=after)
    assert time.strftime("%H:%M", time.localtime(nxt)) == "10:15"


# ---------------------------------------------------------------------------
# procedural memory
# ---------------------------------------------------------------------------


def test_substitute_placeholders():
    assert substitute("sum {{value_column}}", {"value_column": "revenue"}) == "sum revenue"
    assert substitute({"path": "{{file}}", "agg": "sum"}, {"file": "a.csv"}) == {
        "path": "a.csv",
        "agg": "sum",
    }


def test_learn_from_run_generalizes_inputs(ws):
    from sworker.engine import WorkerEngine
    from sworker.config import get_worker
    from sworker.inference import NullInference
    from sworker.tools import build_registry

    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    engine = WorkerEngine(
        worker, store, inference=NullInference(), registry=build_registry()
    )
    result = engine.run("Q2 revenue total?", inputs={"quarter": "Q2"})
    body = learn_from_run(
        store, result.run.id, "q2_total", inputs={"quarter": "Q2"}
    )
    save_procedure(worker, "q2_total", body)
    procs = {p["name"]: p for p in list_procedures(worker)}
    assert "q2_total" in procs
    # The learned procedure must generalize the literal Q2 back to a placeholder.
    assert "{{quarter}}" in body, body
    steps = procedure_steps(procs["q2_total"], {"quarter": "Q2"})
    # Only actually-executed data/fs actions survive into the procedure.
    assert any(s.get("tool") == "data.query" for s in steps)
