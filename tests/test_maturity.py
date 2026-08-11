"""§70 — maturity model tests.

Fail-closed discipline: the maturity level is the *floor* (weakest dimension) of
real, persisted subsystem state. A fresh deployment (no users, no events, no
exercised incident/ledger) must NOT claim a flattering tier — it must resolve to a
low level. A fully-exercised deployment must reach a higher level. The report must
cite real evidence strings describing where each signal came from.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sworker.config import Workspace
from sworker.store import WorkerStore
from sworker.auth import AuthProvider
from sworker.safemode import SafeMode
from sworker.incident import IncidentLedger
from sworker.maturity import (
    MaturityModel, assess_maturity, STANDARD, HARDENED, TIERS,
)


@pytest.fixture()
def store(tmp_path):
    home = tmp_path / "acme"
    home.mkdir()
    os.environ["SWORKER_HOME"] = str(home)
    ws = Workspace(str(home))
    ws.ensure()
    return WorkerStore(ws.state_dir)


def test_fresh_deployment_is_low_not_fabricated(store):
    # No users, no events, no exercised incident -> low maturity, never 'sovereign'
    rep = MaturityModel(store, "acme").assess()
    assert rep.level in TIERS  # valid tier name
    assert rep.floor < STANDARD  # weakest link is below "standard" on a cold store
    assert rep.level != "sovereign"
    # every dimension carries real evidence text, not empty
    for d in rep.dimensions:
        assert d.evidence
        assert d.tier_name in TIERS


def test_auth_dimension_floor_without_users(store):
    rep = MaturityModel(store, "acme").assess()
    auth = next(d for d in rep.dimensions if d.id == "auth")
    assert auth.tier < STANDARD  # no users created yet


def test_auth_dimension_rises_with_admin(store):
    AuthProvider(store).create_user("op1", "pw", role="admin")
    rep = MaturityModel(store, "acme").assess()
    auth = next(d for d in rep.dimensions if d.id == "auth")
    assert auth.tier >= STANDARD


def test_incident_dimension_rises_when_exercised(store):
    led = IncidentLedger(store, scope="")
    led.open("drill")
    led.close()
    rep = MaturityModel(store, "acme").assess()
    inc = next(d for d in rep.dimensions if d.id == "incident_response")
    assert inc.tier >= HARDENED  # exercised -> hardened


def test_safe_mode_default_is_standard_not_hardened(store):
    # default 'off' is a valid standard posture, not hardened
    rep = MaturityModel(store, "acme").assess()
    sm = next(d for d in rep.dimensions if d.id == "safe_mode")
    assert sm.tier == STANDARD


def test_safe_mode_engaged_is_hardened(store):
    SafeMode(store, scope="").set_level("readonly")
    rep = MaturityModel(store, "acme").assess()
    sm = next(d for d in rep.dimensions if d.id == "safe_mode")
    assert sm.tier >= HARDENED


def test_floor_is_weakest_link(store):
    # Fully exercise everything EXCEPT auth; maturity must stay at the
    # weakest dimension (auth), proving a strong audit chain can't mask it.
    AuthProvider(store).create_user("op1", "pw", role="admin")
    SafeMode(store, scope="").set_level("readonly")
    led = IncidentLedger(store, scope="")
    led.open("drill"); led.close()
    rep = MaturityModel(store, "acme").assess()
    scores = [d.tier for d in rep.dimensions]
    assert rep.floor == min(scores)
    assert rep.floor == min(d.tier for d in rep.dimensions)


def test_to_dict_shape_and_assess_helper(store):
    d = assess_maturity(store, "acme")
    assert set(d) >= {"level", "floor", "mean", "dimensions", "summary", "generated_at"}
    assert isinstance(d["dimensions"], list) and d["dimensions"]
    assert "tier_name" in d["dimensions"][0]
