"""Phase 1-3 — Runtime/Worker boundary: domain-independence contract test.

This test is a *guard*, not just a feature test. Its job is to prove the runtime
is genuinely domain-independent so that nobody can later sneak a

    if worker.name == "sales":
        ...

branch into the engine without a test going red.

Two workers with substantially different domains are run through the SAME engine
lifecycle:

  * ``ledger_analyst`` — a core-tool-only worker (fs/data/knowledge). No sales.
  * ``sales_researcher`` — the sales boundary layer (sales_* tools).

Both must:
  - initialize
  - execute a run / procedure
  - use only their permitted tools (registry == subset of declared tools)
  - generate observations
  - generate evidence
  - reach a terminal status
  - persist a run
  - replay the run (explain mode, no model)
  - produce an audit trail

And there must be NO engine code that branches on worker identity.
"""

from __future__ import annotations

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

# A second domain implemented purely with core tools. The point is to show the
# engine treats it exactly like the sales worker: same lifecycle, no special-casing.
LEDGER_ANALYST_YAML = """
name: ledger_analyst
role: Local-first knowledge analyst. Reads permitted files, queries them, and writes a report.
instructions: |
  You operate inside the worker filesystem boundary. Read permitted files with fs.read,
  compute numbers with data.query, and write a report with fs.write.
tools:
  - fs.read
  - fs.list
  - fs.write
  - data.query
  - knowledge.search
policy:
  read: auto
  reversible: auto
  external: approve
  financial: approve
  destructive: approve
fs_roots:
  - data
max_steps: 12
max_actions: 24
"""


def _write_worker(home: Path, name: str, body: str) -> str:
    wdir = home / ".sworker" / "workers"
    wdir.mkdir(parents=True, exist_ok=True)
    p = wdir / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _engine_for(home: Path, name: str) -> WorkerEngine:
    ws = Workspace(str(home))
    ws.ensure()
    worker = load_worker(str(home / ".sworker" / "workers" / f"{name}.yaml"), ws)
    store = WorkerStore(ws.state_dir)
    return WorkerEngine(worker, store, inference=NullInference())


# ---------------------------------------------------------------------------
# The hard guard: the engine must NOT contain domain-specific branching.
# ---------------------------------------------------------------------------
def test_engine_has_no_domain_branching():
    """Static guard. If someone adds `if worker.name == "sales"` to the engine,
    this fails loudly and the PR cannot go green."""
    engine_src = Path(__file__).parent.parent / "sworker" / "engine.py"
    text = engine_src.read_text(encoding="utf-8")
    forbidden = (
        'worker.name == "sales"',
        "worker.name=='sales'",
        "self.worker.name == \"sales\"",
        "name == \"sales\"",
        'getattr(self.worker, "name", "") == "sales"',
    )
    for pat in forbidden:
        assert pat not in text, f"engine contains forbidden domain branch: {pat!r}"
    # Also assert the closed-world planner drops unknown tools generically
    # (not via a sales allow-list baked into the engine).
    assert "unavailable tool" in text


def test_registry_is_bounded_by_worker_allowlist():
    """A worker only ever sees the tools it declared (subset)."""
    home = Path(tempfile.mkdtemp())
    _write_worker(home, "ledger_analyst", LEDGER_ANALYST_YAML)
    eng = _engine_for(home, "ledger_analyst")
    names = set(eng.registry.names())
    # core worker must NOT see any sales tool
    assert not any(n.startswith("sales_") for n in names), names
    assert "fs.read" in names and "data.query" in names
    # subset equality with declared tools
    assert names == set(eng.worker.tools)
    eng.store.close()


def test_sales_worker_registry_excludes_send_tools_for_researcher():
    home = Path(tempfile.mkdtemp())
    _install_researcher(home)
    eng = _engine_for(home, "sales_researcher")
    names = set(eng.registry.names())
    # separation of duties: researcher may not send/approve egress
    assert "sales_record_sent" not in names
    assert "sales_approve_draft" not in names
    assert "sales_discover" in names
    eng.store.close()


# ---------------------------------------------------------------------------
# The lifecycle contract, exercised for BOTH domains.
# ---------------------------------------------------------------------------
def _install_researcher(home: Path) -> None:
    _write_worker(
        home,
        "sales_researcher",
        (Path(TEMPLATES) / "sales_researcher.yaml").read_text(encoding="utf-8"),
    )


def test_core_worker_runs_full_lifecycle():
    """ledger_analyst (no sales) exercises the same runtime lifecycle as sales."""
    home = Path(tempfile.mkdtemp())
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "data" / "notes.md").write_text("# Note\nThe quarter closed at 42 records.\n")
    _write_worker(home, "ledger_analyst", LEDGER_ANALYST_YAML)

    ws = Workspace(str(home))
    ws.ensure()
    worker = load_worker(str(home / ".sworker" / "workers" / "ledger_analyst.yaml"), ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store, inference=NullInference())

    res = eng.run(
        "Read data/notes.md and write a one-line summary to report.md",
        trigger="test",
    )
    run_id = res.run.id
    assert res.run.id
    assert res.run.worker == "ledger_analyst"

    # persisted + terminal
    rec = store.get("runs", run_id)
    assert rec is not None
    from sworker.models import RunStatus

    assert RunStatus(rec["status"]).name in (
        "SUCCESS", "PARTIAL_SUCCESS", "FAILED", "BLOCKED",
        "INSUFFICIENT_EVIDENCE", "DENIED", "CANCELLED",
    )

    # canonical audit events present for BOTH domains
    audit = list(store.iter_audit(run_id))
    kinds = {a["event"] for a in audit}
    assert "run.started" in kinds
    assert "plan.created" in kinds
    assert "run.finished" in kinds

    # observations + evidence produced by the runtime (not the domain)
    obs = store.find("observations", run_id=run_id)
    assert obs, "runtime must record observations for any worker"
    ev = store.find("evidence", run_id=run_id)
    assert ev, "runtime must record evidence for any worker"

    # replay reconstructs without a model
    rep = explain_mod.replay(eng, run_id, mode="explain")
    assert rep, "replay must reconstruct the run"

    store.close()


def test_sales_worker_runs_full_lifecycle():
    """sales_researcher exercises the identical runtime lifecycle as the core worker."""
    home = Path(tempfile.mkdtemp())
    (home / "company").mkdir(parents=True, exist_ok=True)
    ledger_dir = home / "company" / "Experiment_Ledger"
    ledger_dir.mkdir(parents=True)
    db = str(ledger_dir / "experiments.db")
    os.environ["DAILYSALESOS_LEDGER"] = db
    os.environ["DAILYSALESOS_ROOT"] = DAILYSALESOS

    _install_researcher(home)
    ws = Workspace(str(home))
    ws.ensure()
    worker = load_worker(str(home / ".sworker" / "workers" / "sales_researcher.yaml"), ws)
    store = WorkerStore(ws.state_dir)
    eng = WorkerEngine(worker, store, inference=NullInference())

    res = eng.run(
        "Qualify the lead for Acme Realty and report the pipeline",
        trigger="test",
    )
    run_id = res.run.id
    rec = store.get("runs", run_id)
    assert rec is not None and rec["worker"] == "sales_researcher"

    audit = list(store.iter_audit(run_id))
    kinds = {a["event"] for a in audit}
    assert {"run.started", "plan.created", "run.finished"} <= kinds
    obs = store.find("observations", run_id=run_id)
    ev = store.find("evidence", run_id=run_id)
    assert obs and ev

    rep = explain_mod.replay(eng, run_id, mode="explain")
    assert rep

    store.close()
    print(f"OK contract: ledger_analyst + sales_researcher both ran the runtime lifecycle; "
          f"sales run {run_id} has {len(audit)} audit events")
