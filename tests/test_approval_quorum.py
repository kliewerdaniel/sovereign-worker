"""§45 HITL escalation / quorum — fail-closed enforcement.

The historical floor (one human approve settles the action) must survive; the
new behaviour (N distinct approvers at-or-above min_role) must only *add* a
gate, never lower one, and must never hang or silently succeed.
"""

import os
import tempfile

import pytest

from sworker.approvals import ApprovalManager, ApprovalError
from sworker.config import WorkerConfig
from sworker.models import Action, ActionStatus, RiskLevel
from sworker.store import WorkerStore


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return WorkerStore(os.path.join(d, "state"))


def _action(risk=RiskLevel.EXTERNAL):
    return Action(
        run_id="run_1", step_id="step_1", tool="fs.read", risk=risk,
        summary="read report", rationale="needed", args={},
    )


def _worker(approval_policy=None):
    w = WorkerConfig(name="w", workspace=tempfile.mkdtemp())
    if approval_policy is not None:
        w.approval_policy = approval_policy
    return w


# -- default floor preserved ------------------------------------------------
def test_single_approver_settles_by_default(store):
    mgr = ApprovalManager(store)
    a = _action()
    appr = mgr.request(a, summary="x", reason="y")
    assert appr.quorum == 1 and appr.min_role == ""
    rec = mgr.approve(appr.id, by="alice")
    assert rec["state"] == "APPROVED"
    assert a.approval_id == appr.id
    # action record flipped to APPROVED
    assert store.get("actions", a.id)["status"] == ActionStatus.APPROVED.value


def test_single_reject_settles(store):
    mgr = ApprovalManager(store)
    appr = mgr.request(_action(), summary="x", reason="y")
    rec = mgr.reject(appr.id, by="alice")
    assert rec["state"] == "REJECTED"


def test_closed_approval_is_immutable(store):
    mgr = ApprovalManager(store)
    appr = mgr.request(_action(), summary="x", reason="y")
    mgr.approve(appr.id, by="alice")
    with pytest.raises(ApprovalError):
        mgr.reject(appr.id, by="bob")  # already settled


# -- quorum ----------------------------------------------------------------
def test_quorum_requires_distinct_approvers(store):
    w = _worker({"external": {"quorum": 2, "min_role": ""}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    assert appr.quorum == 2
    # first vote does not settle
    rec = mgr.approve(appr.id, by="alice")
    assert rec["state"] == "PENDING"
    # same person again does not advance the distinct count
    rec = mgr.approve(appr.id, by="alice")
    assert rec["state"] == "PENDING"
    approvers = [v for v in rec["votes"] if v["state"] == "APPROVED"]
    assert len(approvers) == 1
    # second distinct approver settles
    rec = mgr.approve(appr.id, by="bob")
    assert rec["state"] == "APPROVED"


def test_revote_changes_stance_not_count(store):
    w = _worker({"external": {"quorum": 2, "min_role": ""}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    mgr.approve(appr.id, by="alice")
    mgr.approve(appr.id, by="alice", note="changed my mind")  # still only 1 distinct
    rec = mgr.approve(appr.id, by="bob")
    assert rec["state"] == "APPROVED"
    assert len([v for v in rec["votes"] if v["by"] == "alice"]) == 1


# -- single reject blocks regardless of quorum -----------------------------
def test_single_reject_blocks_quorum(store):
    w = _worker({"external": {"quorum": 3, "min_role": ""}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    mgr.approve(appr.id, by="alice")
    rec = mgr.reject(appr.id, by="bob")  # one no is enough
    assert rec["state"] == "REJECTED"
    # further approvals cannot resurrect it
    with pytest.raises(ApprovalError):
        mgr.approve(appr.id, by="carol")


# -- min_role gate (fail-closed) -------------------------------------------
def test_under_role_vote_refused(store):
    w = _worker({"external": {"quorum": 1, "min_role": "operator"}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    with pytest.raises(ApprovalError):
        mgr.approve(appr.id, by="alice", role="analyst")
    # approval stays PENDING; under-privileged vote not counted
    assert mgr.get(appr.id)["state"] == "PENDING"


def test_role_at_minimum_ok(store):
    w = _worker({"external": {"quorum": 1, "min_role": "operator"}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    rec = mgr.approve(appr.id, by="alice", role="operator")
    assert rec["state"] == "APPROVED"


def test_admin_satisfies_any_min_role(store):
    w = _worker({"external": {"quorum": 1, "min_role": "admin"}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    rec = mgr.approve(appr.id, by="root", role="admin")
    assert rec["state"] == "APPROVED"


# -- escalation (structural) ------------------------------------------------
def test_escalate_raises_quorum(store):
    w = _worker({"external": {"quorum": 2, "min_role": ""}})
    mgr = ApprovalManager(store, worker=w)
    appr = mgr.request(_action(), summary="x", reason="y")
    rec = mgr.escalate(appr.id, by="engine")
    assert rec["quorum"] == 3 and rec["escalations"] == 1
    assert rec["state"] == "PENDING"


# -- config parsing --------------------------------------------------------
def test_approval_policy_parsed_from_yaml():
    text = """
name: acme-gated
approval_policy:
  destructive:
    quorum: 2
    min_role: operator
  financial:
    quorum: 1
    min_role: ""
"""
    import io
    from sworker.config import load_worker
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    p.write(text)
    p.close()
    w = load_worker(p.name)
    assert w.approval_policy_for(RiskLevel.DESTRUCTIVE) == {"quorum": 2, "min_role": "operator"}
    assert w.approval_policy_for(RiskLevel.FINANCIAL) == {"quorum": 1, "min_role": ""}
    # absent risk -> floor
    assert w.approval_policy_for(RiskLevel.EXTERNAL) == {"quorum": 1, "min_role": ""}


def test_quorum_never_drops_below_one():
    w = _worker({"external": {"quorum": 0, "min_role": ""}})
    assert w.approval_policy_for(RiskLevel.EXTERNAL)["quorum"] == 1
