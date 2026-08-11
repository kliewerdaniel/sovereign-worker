"""Run state machine (spec §12).

A run's status is not a free-form string the engine scatters assignments across
— it is a finite state machine with explicit, enforced transitions. An illegal
transition (e.g. SUCCESS -> EXECUTING, or CANCELLED -> RUNNING) is a bug the
platform must refuse, not a value we quietly accept. Every transition is
persisted as an audit event with who + why, so the state history is reconstructable.

Fail-closed: an unknown target state or a transition absent from the table raises
``IllegalTransition``. There is no silent "just set it" path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .models import RunStatus


class IllegalTransition(Exception):
    """Raised when code tries to move a run to a state it cannot reach."""


# Allowed transitions. A state maps to the set of states it may move to (or the
# special token ANY_TERMINAL to mean "any terminal state"). Absent key => no
# outgoing transitions (terminal).
class _T:
    ANY_TERMINAL = "*TERMINAL*"  # type: ignore[assignment]


_TERMINAL = {
    RunStatus.SUCCESS,
    RunStatus.PARTIAL_SUCCESS,
    RunStatus.FAILED,
    RunStatus.BLOCKED,
    RunStatus.INSUFFICIENT_EVIDENCE,
    RunStatus.CANCELLED,
    RunStatus.DENIED,
}

TRANSITIONS: Dict[RunStatus, Any] = {
    RunStatus.PENDING: {RunStatus.PLANNING, RunStatus.CANCELLED, RunStatus.DENIED},
    RunStatus.PLANNING: {RunStatus.EXECUTING, RunStatus.CANCELLED, RunStatus.DENIED, RunStatus.INSUFFICIENT_EVIDENCE, RunStatus.BLOCKED},
    RunStatus.EXECUTING: {
        RunStatus.AWAITING_APPROVAL,
        RunStatus.VERIFYING,
        RunStatus.CANCELLED,
        RunStatus.DENIED,
        RunStatus.BLOCKED,
        RunStatus.FAILED,
    },
    RunStatus.AWAITING_APPROVAL: {RunStatus.EXECUTING, RunStatus.CANCELLED, RunStatus.DENIED, RunStatus.BLOCKED},
    RunStatus.VERIFYING: {
        *_TERMINAL,
    },
    # terminal states have no outgoing transitions
}


@dataclass
class TransitionRecord:
    run_id: str
    from_state: str
    to_state: str
    actor: str
    reason: str
    at: float = field(default_factory=time.time)


def is_terminal(state: RunStatus) -> bool:
    return state in _TERMINAL


def allowed_transition(current: RunStatus, target: RunStatus) -> bool:
    out = TRANSITIONS.get(current, set())
    if _T.ANY_TERMINAL in out:
        return target in _TERMINAL
    return target in out


def transition(
    run: Any,
    target: RunStatus,
    store: Any = None,
    actor: str = "engine",
    reason: str = "",
) -> RunStatus:
    """Move ``run`` to ``target``, enforcing the state machine.

    ``run`` may be a ``Run`` dataclass or a dict with ``status``. The new status
    is written back into ``run.status``. If ``store`` is supplied, the transition
    is persisted as a ``run.transition`` audit event and the run record is
    updated. Illegal transitions raise ``IllegalTransition`` (fail-closed).
    """
    current = RunStatus(run.status)
    if current == target:
        return target
    if not allowed_transition(current, target):
        raise IllegalTransition(
            f"cannot move run {getattr(run, 'id', '?')} from {current.value} to {target.value}"
        )
    run.status = target
    if store is not None:
        rid = getattr(run, "id", "")
        rec = TransitionRecord(
            run_id=rid,
            from_state=current.value,
            to_state=target.value,
            actor=actor,
            reason=reason,
        )
        store.audit("run.transition", "runs", rid, rec.__dict__)
        # reflect the new status on the persisted run record
        persisted = store.get("runs", rid) if rid else None
        if persisted is not None:
            persisted["status"] = target.value
            store.put("runs", persisted, event="run.status")
    return target


def can_cancel(state: RunStatus) -> bool:
    """Cancellation aborts any non-terminal run."""
    return not is_terminal(state)
