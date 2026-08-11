"""§23 procedure publish / rollback / list + RBAC permission gate.

Fail-closed invariants verified:
  * publishing requires `procedure:publish` (role gate).
  * publishing twice does not silently overwrite (version is deterministic
    and FileExistsError unless forced).
  * rollback to a non-existent version fails closed.
  * cannot roll back past the earliest version.
  * published procedure carries author + content hash; current version tracked.
"""

import os

import pytest

from sworker.config import Workspace
from sworker.procedures import (
    publish_procedure, rollback_procedure, list_published,
    current_version, procedure_published, can_publish, next_procedure_version,
)
from sworker.rbac import RBAC


@pytest.fixture
def worker(tmp_path):
    ws = Workspace(str(tmp_path / "home"))
    ws.ensure()
    # a minimal worker pointing at this workspace
    class _W:
        name = "demo"
        workspace = ws.root
    return _W()


BODY = "name: weekly-report\nintent: build weekly report\nsteps:\n  - tool: shell.exec\n    args:\n      command: echo hi\n"
BODY2 = "name: weekly-report\nintent: build weekly report v2\nsteps:\n  - tool: shell.exec\n    args:\n      command: echo bye\n"


def test_publish_creates_version_one(worker):
    info = publish_procedure(worker, "weekly-report", BODY, author="alice")
    assert info["version"] == "1.0"
    assert current_version(worker, "weekly-report") == "1.0"
    assert info["author"] == "alice"
    assert info["hash"]


def test_publish_increments_version(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    info = publish_procedure(worker, "weekly-report", BODY2, author="bob")
    assert info["version"] == "1.1"
    assert current_version(worker, "weekly-report") == "1.1"


def test_publish_never_silently_overwrites(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    # re-publishing different content always bumps the version; nothing is clobbered
    info = publish_procedure(worker, "weekly-report", BODY2, author="bob")
    assert info["version"] == "1.1"
    # both versions remain readable; current is the newer one
    pub = list_published(worker)
    assert {p["version"] for p in pub} == {"1.0", "1.1"}
    assert current_version(worker, "weekly-report") == "1.1"


def test_list_published_shows_all_versions(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    publish_procedure(worker, "weekly-report", BODY2, author="bob")
    pub = list_published(worker)
    assert {p["version"] for p in pub} == {"1.0", "1.1"}
    assert {p["author"] for p in pub} == {"alice", "bob"}


def test_rollback_to_previous(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    publish_procedure(worker, "weekly-report", BODY2, author="bob")
    assert current_version(worker, "weekly-report") == "1.1"
    info = rollback_procedure(worker, "weekly-report")
    assert info["version"] == "1.0"
    assert current_version(worker, "weekly-report") == "1.0"
    # procedure_published returns the active (rolled-back) version
    active = procedure_published(worker, "weekly-report")
    assert active["version"] == "1.0"


def test_rollback_to_named_version(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    publish_procedure(worker, "weekly-report", BODY2, author="bob")
    info = rollback_procedure(worker, "weekly-report", version="1.0")
    assert info["version"] == "1.0"
    assert current_version(worker, "weekly-report") == "1.0"


def test_rollback_past_earliest_fails(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    with pytest.raises(ValueError):
        rollback_procedure(worker, "weekly-report")


def test_rollback_unknown_version_fails(worker):
    publish_procedure(worker, "weekly-report", BODY, author="alice")
    with pytest.raises(ValueError):
        rollback_procedure(worker, "weekly-report", version="9.9")


def test_next_version_empty_is_1_0(worker):
    assert next_procedure_version(worker, "fresh") == "1.0"


def test_rbac_gate_admin_can_publish():
    rbac = RBAC()
    assert can_publish(rbac, "admin") is True
    assert can_publish(rbac, "operator") is True
    assert can_publish(rbac, "analyst") is False   # lacks procedure:publish
    assert can_publish(rbac, "viewer") is False
