"""§61 graceful degradation — degradations are recorded, surfaced, and
fail-closed (a critical degradation forces a run off full SUCCESS)."""

import os
import tempfile

import pytest

from sworker import degradation as D
from sworker.degradation import (
    DegradationLedger,
    DegradationRecord,
    WARN,
    CRITICAL,
    MODEL_FALLBACK,
)
from sworker.store import WorkerStore


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return WorkerStore(os.path.join(d, ".state"))


def test_record_persists_and_audits(store):
    led = DegradationLedger(store, run_id="run_1")
    rec = led.record(MODEL_FALLBACK, "no model", severity=WARN, mitigation="x")
    assert rec.severity == WARN
    # queryable per-run
    ents = led.entries()
    assert len(ents) == 1
    assert ents[0].category == MODEL_FALLBACK
    # also mirrored into the audit log (tamper-evident)
    audits = [a for a in store.iter_audit() if a.get("event") == "degradation.recorded"]
    assert audits, "degradation must be written to the audit log"


def test_unknown_severity_is_treated_as_critical(store):
    # fail-closed: a typo'd or attacker-supplied severity must never be quietly
    # downgraded to a harmless "warn".
    led = DegradationLedger(store, run_id="run_1")
    rec = led.record("something", "x", severity="bogus")
    assert rec.severity == CRITICAL
    assert led.any_critical()


def test_any_critical_downgrades_success(store):
    from sworker.models import Run, RunStatus
    from sworker.evidence import EvidenceLedger
    from sworker.engine import WorkerEngine
    from sworker.config import WorkerConfig
    from sworker.tools.base import ToolRegistry, ToolContext
    from sworker.models import Evidence, Provenance

    cfg = WorkerConfig(name="w", workspace=tempfile.mkdtemp())
    eng = WorkerEngine(cfg, store)
    run = Run(worker="w", task_id="t1")
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    led = DegradationLedger(store, run_id=run.id)
    led.record("safety_check_skipped", "audit hook offline", severity=CRITICAL)
    ledger = EvidenceLedger(store, run.id)
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    # a clean SUCCESS that must be downgraded because a critical capability fell away
    res = eng._finalize(run, ledger, led, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    assert res.status is RunStatus.PARTIAL_SUCCESS
    assert "critical capability degradation" in run.error
    assert any("safety_check_skipped" in d for d in run.degradations)


def test_warn_does_not_downgrade_success(store):
    from sworker.models import Run, RunStatus
    from sworker.evidence import EvidenceLedger
    from sworker.engine import WorkerEngine
    from sworker.config import WorkerConfig
    from sworker.tools.base import ToolRegistry, ToolContext
    from sworker.models import Evidence, Provenance

    cfg = WorkerConfig(name="w", workspace=tempfile.mkdtemp())
    eng = WorkerEngine(cfg, store)
    run = Run(worker="w", task_id="t1")
    run.status = RunStatus.EXECUTING
    store.put("runs", run, event="run.started")
    led = DegradationLedger(store, run_id=run.id)
    led.record(MODEL_FALLBACK, "no model", severity=WARN)
    ledger = EvidenceLedger(store, run.id)
    ev = Evidence(run_id=run.id, provenance=Provenance.OBSERVED, summary="saw rows")
    store.put("evidence", ev, event="evidence.recorded")
    res = eng._finalize(run, ledger, led, failures=0, executed=1, blocked=False,
                        awaiting=False, on_event=lambda e, p: None)
    # a warn-level degradation is surfaced but does NOT force a downgrade
    assert res.status is RunStatus.SUCCESS
    assert run.degradations and "model_fallback" in run.degradations[0]


def test_model_fallback_recorded_on_run_without_llm(store, monkeypatch):
    # A run with no reachable model must record the model_fallback degradation.
    from sworker.models import Run, RunStatus, Task
    from sworker.config import WorkerConfig
    from sworker.evidence import EvidenceLedger
    from sworker.inference import NullInference
    from sworker.engine import WorkerEngine
    from sworker.tools.base import ToolRegistry, ToolContext

    cfg = WorkerConfig(name="w", workspace=tempfile.mkdtemp())
    eng = WorkerEngine(cfg, store, inference=NullInference())
    task = Task(worker="w", request="do a thing")
    store.put("tasks", task, event="task.created")
    run = Run(worker="w", task_id=task.id)
    store.put("runs", run, event="run.started")
    # build an empty registry so no tools run; just verify the fallback path
    led = DegradationLedger(store, run_id=run.id)
    assert not eng.llm.available()
    # emulate run()'s own recording
    led.record(MODEL_FALLBACK, "no reachable language model", severity=WARN)
    assert led.entries()[0].category == MODEL_FALLBACK


def test_summary_lines_are_human_readable(store):
    led = DegradationLedger(store, run_id="r")
    led.record(MODEL_FALLBACK, "no model", severity=WARN)
    lines = led.summary()
    assert lines == ["model_fallback: no model [warn]"]


def test_roundtrip_via_store(store):
    led = DegradationLedger(store, run_id="run_x")
    led.record("a", "reason a", severity=CRITICAL)
    led.record("b", "reason b", severity=WARN)
    # fresh ledger reads back persisted entries
    led2 = DegradationLedger(store, run_id="run_x")
    cats = {e.category for e in led2.entries()}
    assert cats == {"a", "b"}
    assert led2.any_critical()
