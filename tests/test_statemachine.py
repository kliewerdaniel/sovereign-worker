"""Run state machine (spec §12).

Transitions must be enforced: legal moves succeed, illegal ones raise
``IllegalTransition`` (fail-closed), and each transition is recorded in the
audit ledger so the state history is reconstructable.
"""

from __future__ import annotations

import pytest

from sworker.models import Run, RunStatus
from sworker.store import WorkerStore
from sworker.statemachine import (
    IllegalTransition,
    allowed_transition,
    can_cancel,
    is_terminal,
    transition,
)


def _run(status=RunStatus.PENDING) -> Run:
    return Run(worker="x", task_id="t", status=status)


def test_happy_path():
    r = _run()
    assert transition(r, RunStatus.PLANNING) == RunStatus.PLANNING
    assert transition(r, RunStatus.EXECUTING) == RunStatus.EXECUTING
    assert transition(r, RunStatus.VERIFYING) == RunStatus.VERIFYING
    assert transition(r, RunStatus.SUCCESS) == RunStatus.SUCCESS
    assert is_terminal(r.status)


def test_awaiting_approval_loop():
    r = _run(RunStatus.EXECUTING)
    transition(r, RunStatus.AWAITING_APPROVAL)
    # resume back into executing
    transition(r, RunStatus.EXECUTING)
    transition(r, RunStatus.VERIFYING)
    transition(r, RunStatus.SUCCESS)
    assert r.status == RunStatus.SUCCESS


def test_illegal_terminal_to_executing():
    r = _run(RunStatus.SUCCESS)
    with pytest.raises(IllegalTransition):
        transition(r, RunStatus.EXECUTING)


def test_illegal_pending_to_verifying():
    r = _run(RunStatus.PENDING)
    with pytest.raises(IllegalTransition):
        transition(r, RunStatus.VERIFYING)


def test_cancelled_is_terminal_and_blocks_further():
    r = _run(RunStatus.EXECUTING)
    transition(r, RunStatus.CANCELLED)
    assert is_terminal(r.status)
    with pytest.raises(IllegalTransition):
        transition(r, RunStatus.EXECUTING)


def test_noop_same_state_ok():
    r = _run(RunStatus.EXECUTING)
    assert transition(r, RunStatus.EXECUTING) == RunStatus.EXECUTING


def test_transition_persisted_to_audit(tmp_path):
    store = WorkerStore(str(tmp_path / "w"))
    r = Run(worker="x", task_id="t", status=RunStatus.PENDING)
    store.put("runs", r, event="run.started")
    transition(r, RunStatus.PLANNING, store=store, actor="engine", reason="planning")
    transition(r, RunStatus.EXECUTING, store=store, actor="engine", reason="exec")
    # persisted run reflects latest status
    persisted = store.get("runs", r.id)
    assert persisted["status"] == RunStatus.EXECUTING.value
    # a transition event exists in the ledger
    kinds = [rec["event"] for rec in store.iter_audit(r.id)]
    assert "run.transition" in kinds


def test_all_terminal_states_have_no_outgoing():
    for st in (
        RunStatus.SUCCESS,
        RunStatus.PARTIAL_SUCCESS,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.INSUFFICIENT_EVIDENCE,
        RunStatus.CANCELLED,
        RunStatus.DENIED,
    ):
        assert is_terminal(st)
        assert not allowed_transition(st, RunStatus.PLANNING)


def test_can_cancel_non_terminal():
    assert can_cancel(RunStatus.EXECUTING)
    assert can_cancel(RunStatus.PLANNING)
    assert not can_cancel(RunStatus.CANCELLED)
    assert not can_cancel(RunStatus.SUCCESS)
