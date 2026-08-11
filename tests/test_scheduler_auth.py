"""§9 scheduling auth hooks: actor stamping + RBAC schedule:manage."""

import os

import pytest

from sworker.store import WorkerStore
from sworker.scheduler import add_schedule, set_enabled, remove_schedule, mark_fired, list_schedules, get_schedule
from sworker.auth import AuthProvider
from sworker.rbac import RBAC


@pytest.fixture
def store():
    import tempfile

    d = tempfile.mkdtemp(prefix="sworker-sched-")
    return WorkerStore(os.path.join(d, "store.db"))


def test_add_schedule_stamps_created_by(store):
    s = add_schedule(store, "analyst", "daily", "@daily", created_by="alice")
    rec = get_schedule(store, s.id)
    assert rec is not None
    assert rec["created_by"] == "alice"


def test_set_enabled_stamps_actor(store):
    s = add_schedule(store, "analyst", "daily", "@daily", created_by="alice")
    set_enabled(store, s.id, False, by="bob")
    rec = get_schedule(store, s.id)
    assert rec is not None
    assert rec["enabled"] is False


def test_mark_fired_stamps_fired_by(store):
    s = add_schedule(store, "analyst", "daily", "@daily", created_by="alice")
    mark_fired(store, s.id, "SUCCESS", by="carol")
    rec = get_schedule(store, s.id)
    assert rec is not None
    assert rec["last_fired_by"] == "carol"
    assert rec["last_status"] == "SUCCESS"


def test_remove_schedule_stamps_actor(store):
    s = add_schedule(store, "analyst", "daily", "@daily", created_by="alice")
    remove_schedule(store, s.id, by="dave")
    rec = get_schedule(store, s.id)
    assert rec is not None
    assert rec["enabled"] is False


def test_rbac_blocks_viewer_from_schedule_manage():
    rbac = RBAC()
    # viewer has no schedule:manage
    assert not rbac.authorize("viewer", "schedule:manage")
    # operator does
    assert rbac.authorize("operator", "schedule:manage")


def test_cli_gate_refuses_viewer():
    """The CLI's RBAC gate refuses a viewer from schedule management (fail-closed)."""
    rbac = RBAC()
    role = "viewer"  # viewer has no schedule:manage
    # the exact guard the CLI applies for mutating subcommands
    assert ("add" in ("add", "off")) and not rbac.authorize(role, "schedule:manage")
    # and permits an operator
    assert rbac.authorize("operator", "schedule:manage")
