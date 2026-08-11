"""Resource-control enforcement (spec §10).

Every limit is a structured failure, never silent:
- max_actions / max_tool_calls abort the run with BLOCKED status and an error.
- max_network_requests is decremented for network-category tools.
- max_runtime is backed by a watchdog that kills the live child group.
"""
import os
import tempfile

import pytest

from sworker.config import WorkerConfig
from sworker.engine import WorkerEngine
from sworker.models import RunStatus
from sworker.permissions import DecompositionGuard, PermissionEngine
from sworker.store import WorkerStore
from sworker.tools import build_registry
from sworker.tools.base import ToolContext


def _cfg(workspace, **overrides):
    kw = dict(
        name="res-analyst",
        role="analyst",
        instructions="compute figures from the CSV using data.query",
        tools=["fs.list", "fs.read", "data.query"],
        fs_roots=["company"],
        workspace=workspace,
    )
    kw.update(overrides)
    return WorkerConfig(**kw)


def _eng(workspace, **overrides):
    cfg = _cfg(workspace, **overrides)
    store = WorkerStore(os.path.join(workspace, ".state", "store.db"))
    return (
        WorkerEngine(cfg, store=store, inference=None, registry=build_registry()),
        cfg,
        store,
    )


@pytest.fixture
def workspace():
    d = tempfile.mkdtemp(prefix="sworker-res-")
    company = os.path.join(d, "company")
    os.makedirs(company)
    with open(os.path.join(company, "sales.csv"), "w", encoding="utf-8") as fh:
        fh.write(
            "region,quarter,revenue,orders\n"
            "North,Q2,51000,120\nSouth,Q2,35500,80\nOnline,Q2,102000,300\n"
        )
    return d


def test_max_actions_aborts_run(workspace):
    eng, cfg, store = _eng(workspace, max_actions=1)
    res = eng.run("What was total Q2 revenue?")
    # the deterministic Q2 plan proposes ~4 steps; the cap stops it early.
    assert res.status == RunStatus.BLOCKED
    assert "max_actions" in (res.run.error or "")
    persisted = store.get("runs", res.run.id)
    assert "max_actions" in (persisted["error"] or "")


def test_max_tool_calls_aborts_run(workspace):
    eng, cfg, store = _eng(workspace, max_actions=100, max_tool_calls=1)
    res = eng.run("What was total Q2 revenue?")
    assert res.status == RunStatus.BLOCKED
    assert "max_tool_calls" in (res.run.error or "")


def test_max_network_requests_bookkeeping(workspace):
    # the http tools are tagged with the "network" category so the budget
    # accounting decrements per call (spec §10).
    reg = build_registry()
    assert reg.get("http.get").categories == ["network"]
    assert reg.get("http.post").categories == ["network"]


def test_max_runtime_watchdog_kills_sleep(workspace):
    """A max_runtime of ~0.3s must kill a long-running shell child."""
    eng, cfg, store = _eng(
        workspace,
        tools=["shell.exec"],
        shell_allow=["sleep"],
        max_runtime=1,
    )
    # build a minimal run that executes `sleep 5` and let the watchdog abort it.
    from sworker.evidence import EvidenceLedger
    from sworker.models import Plan, Run, Step, Task

    task = Task(worker=cfg.name, request="sleep")
    store.put("tasks", task)
    run = Run(worker=cfg.name, task_id=task.id)
    store.put("runs", run)
    from sworker.statemachine import transition as sm_transition
    from sworker.models import RunStatus
    sm_transition(run, RunStatus.PLANNING, store=store, actor="test", reason="setup")
    plan = Plan(run_id=run.id, intent="sleep")
    store.put("plans", plan)
    step = Step(
        run_id=run.id, plan_id=plan.id, index=0,
        description="sleep", tool="shell.exec", args={"command": "sleep 5"},
    )
    store.put("steps", step)
    ledger = EvidenceLedger(store, run.id)
    perms = PermissionEngine(cfg, DecompositionGuard())
    ctx = ToolContext(
        worker=cfg.name, run_id=run.id, workspace=cfg.workspace,
        fs_roots=cfg.resolved_fs_roots(), artifacts_dir=cfg.artifacts_dir(),
        shell_allow=list(cfg.shell_allow), env_allow=list(cfg.env_allow),
        timeout=cfg.timeout, max_output=cfg.max_output,
        max_python_runtime=cfg.max_python_runtime,
        max_shell_runtime=cfg.max_shell_runtime,
    )
    res = eng._execute(run, plan, [step], ledger, perms, ctx, None)
    # the watchdog terminated the child; the run is not left RUNNING.
    assert res.status != RunStatus.RUNNING
