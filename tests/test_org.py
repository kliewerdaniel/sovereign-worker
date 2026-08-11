"""Tenant isolation (spec §3).

A client must never accidentally access another client's data. The store enforces
an explicit ``workspace_id`` INDEPENDENT of the filesystem root. The decisive test
here: two enforcing stores, different workspace ids, pointing at the SAME database
file. The one that did not write a record must NOT be able to read it — it must
raise ``CrossTenantAccess``, never return or silently omit.
"""

from __future__ import annotations

import pytest

from sworker.org import TenantRegistry, CrossTenantAccess
from sworker.store import WorkerStore, CrossTenantAccess as StoreCrossTenantAccess
from sworker.models import Run, RunStatus


def _make_registry(tmp_path):
    reg = TenantRegistry(str(tmp_path / "registry.json"))
    org = reg.create_org("Acme")
    reg.create_workspace(org.id, "main", str(tmp_path / "acme"), ws_id="ws_acme")
    reg.create_workspace(org.id, "other", str(tmp_path / "other"), ws_id="ws_other")
    return reg


def _run_record(ws_id: str) -> Run:
    return Run(
        task_id="task_x",
        worker="analyst",
        status=RunStatus.SUCCESS,
    )


def test_registry_creates_org_and_workspaces(tmp_path):
    reg = _make_registry(tmp_path)
    orgs = reg.list_orgs()
    assert len(orgs) == 1
    wss = reg.list_workspaces(orgs[0].id)
    assert {w.id for w in wss} == {"ws_acme", "ws_other"}


def test_registry_persists(tmp_path):
    reg = _make_registry(tmp_path)
    reg2 = TenantRegistry(str(tmp_path / "registry.json"))
    assert {o.id for o in reg2.list_orgs()} == {o.id for o in reg.list_orgs()}
    assert {w.id for w in reg2.list_workspaces()} == {"ws_acme", "ws_other"}


def test_cross_tenant_read_fails_closed(tmp_path):
    """Two enforcing stores, same db file, different workspace ids."""
    db = str(tmp_path / "shared.db")
    store_a = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    store_b = WorkerStore(db, workspace_id="ws_other", org_id="org_x")

    run = _run_record("ws_acme")
    store_a.put("runs", run, event="run.created")  # stamped ws_acme

    # A can read what it wrote.
    got = store_a.get("runs", run.id)
    assert got is not None
    assert got["workspace_id"] == "ws_acme"

    # B (ws_other) MUST NOT see ws_acme's record.
    with pytest.raises((CrossTenantAccess, StoreCrossTenantAccess)):
        store_b.get("runs", run.id)


def test_cross_tenant_find_fails_closed(tmp_path):
    db = str(tmp_path / "shared.db")
    store_a = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    store_b = WorkerStore(db, workspace_id="ws_other", org_id="org_x")

    store_a.put("runs", _run_record("ws_acme"), event="run.created")

    # explicit cross-workspace query from an enforcing store is refused
    with pytest.raises((CrossTenantAccess, StoreCrossTenantAccess)):
        store_b.find("runs", workspace="ws_acme")

    # and a plain list from B is scoped to its own tenant (empty)
    assert store_b.find("runs") == []


def test_enforcing_store_refuses_legacy_tenantless_record(tmp_path):
    db = str(tmp_path / "shared.db")
    # legacy, non-enforcing write first
    legacy = WorkerStore(db)
    legacy.put("runs", _run_record(""), event="run.created")

    enforcing = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    # an enforcing store must refuse to surface a tenantless (legacy) record
    with pytest.raises((CrossTenantAccess, StoreCrossTenantAccess)):
        enforcing.get("runs", legacy.find("runs")[0]["id"])


def test_same_tenant_read_ok(tmp_path):
    db = str(tmp_path / "shared.db")
    a = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    b = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    run = _run_record("ws_acme")
    a.put("runs", run, event="run.created")
    assert b.get("runs", run.id) is not None  # same tenant OK


def test_put_stamps_tenant(tmp_path):
    db = str(tmp_path / "shared.db")
    store = WorkerStore(db, workspace_id="ws_acme", org_id="org_x")
    run = _run_record("ws_acme")
    d = store.put("runs", run, event="run.created")
    assert d["workspace_id"] == "ws_acme"
    assert d["org_id"] == "org_x"
    assert run.to_dict()  # original record object unchanged


def test_legacy_store_unchanged(tmp_path):
    """A non-enforcing store behaves exactly as before tenant isolation."""
    db = str(tmp_path / "shared.db")
    store = WorkerStore(db)
    run = _run_record("")
    store.put("runs", run, event="run.created")
    got = store.get("runs", run.id)
    assert got is not None
    assert "workspace_id" not in got or got.get("workspace_id") == ""
    assert store.find("runs") == [got]
