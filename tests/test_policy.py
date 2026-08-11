"""Versioned immutable policy tests (spec §6)."""

import pytest

from sworker.policy import PolicyStore, make_policy
from sworker.store import WorkerStore


@pytest.fixture
def store():
    import tempfile, os

    d = tempfile.mkdtemp(prefix="sworker-pol-")
    return WorkerStore(os.path.join(d, "store.db"))


@pytest.fixture
def ps(store):
    return PolicyStore(store)


def _body(read="auto", reversible="auto", external="approve", financial="approve", destructive="deny"):
    return {
        "read": read, "reversible": reversible, "external": external,
        "financial": financial, "destructive": destructive,
    }


def test_publish_creates_version_1(ps):
    p = ps.publish(_body(), "workspace:acme", actor="alice")
    assert p.version == 1
    assert ps.latest("workspace:acme").hash == p.hash


def test_publish_increments_version(ps):
    p1 = ps.publish(_body(), "workspace:acme")
    p2 = ps.publish(_body(read="approve"), "workspace:acme")
    assert p2.version == 2
    assert p1.hash != p2.hash
    # latest points at the new version
    assert ps.latest("workspace:acme").hash == p2.hash


def test_identical_policy_reuses_hash(ps):
    p1 = ps.publish(_body(), "workspace:acme")
    p2 = ps.publish(_body(), "workspace:acme")
    # identical body -> same content hash, no spurious new version
    assert p1.hash == p2.hash
    assert p2.version == 1


def test_immutability_bodies_never_edited(ps):
    p = ps.publish(_body(), "workspace:acme")
    stored = ps.get(p.hash)
    assert stored.body["destructive"] == "deny"
    # a later publish with a different body is a *new* hash, old one intact
    ps.publish(_body(destructive="approve"), "workspace:acme")
    assert ps.get(p.hash).body["destructive"] == "deny"


def test_run_captures_policy_version(store, ps):
    p = ps.publish(_body(), "workspace:acme")
    # a run records the policy hash it ran under (audit reproducibility)
    run_rec = {"id": "run_1", "policy_hash": p.hash, "policy_version": p.version}
    store.put("runs", run_rec, event="run.started")
    got = store.get("runs", "run_1")
    assert got["policy_hash"] == p.hash
    assert got["policy_version"] == 1


def test_scopes_are_independent(ps):
    a = ps.publish(_body(), "workspace:acme")
    b = ps.publish(_body(read="deny"), "workspace:other")
    assert a.hash != b.hash
    assert ps.latest("workspace:acme").body["read"] == "auto"
    assert ps.latest("workspace:other").body["read"] == "deny"
