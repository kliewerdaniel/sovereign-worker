"""§62 safe mode — a single switch that makes the worker fail closed.

Tests cover the ``SafeMode`` controller (levels, fail-closed read, persistence,
decision logic) and its integration into ``WorkerEngine`` (a run executed under
``readonly`` / ``locked`` is blocked and records a critical degradation rather
than being reported as a clean SUCCESS).
"""

import os
import tempfile

import pytest

from sworker import safemode as S
from sworker.models import RiskLevel
from sworker.safemode import SafeMode, OFF, READONLY, LOCKED, SAFE_MODE_BLOCK
from sworker.store import WorkerStore


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return WorkerStore(os.path.join(d, ".state"))


# --- controller unit -------------------------------------------------------

def test_default_level_is_off(store):
    sm = SafeMode(store)
    assert sm.level() == OFF
    assert not sm.enabled()


def test_enable_disable_roundtrip(store):
    sm = SafeMode(store)
    assert sm.enable() == READONLY
    assert sm.enabled()
    assert sm.level() == READONLY
    sm.disable()
    assert sm.level() == OFF
    assert not sm.enabled()


def test_explicit_levels(store):
    sm = SafeMode(store)
    assert sm.set_level(LOCKED) == LOCKED
    assert sm.level() == LOCKED
    assert sm.set_level(READONLY) == READONLY
    assert sm.level() == READONLY


def test_unknown_level_rejected(store):
    sm = SafeMode(store)
    with pytest.raises(ValueError):
        sm.set_level("explode")


def test_corrupt_persisted_level_fails_closed_to_locked(store):
    # A bad/unknown persisted value must only ever increase restriction, never
    # silently disable the guard.
    sm = SafeMode(store)
    sm.set_level(READONLY)
    # simulate corruption of the stored value
    store.put("meta_kv", {"id": "safemode:level:safemode", "scope": "safemode", "level": "bogus"}, event="test")
    assert SafeMode(store).level() == LOCKED
    assert SafeMode(store).enabled()


def test_readonly_blocks_above_read(store):
    sm = SafeMode(store)
    sm.set_level(READONLY)
    assert not sm.is_blocked(RiskLevel.READ)
    assert sm.is_blocked(RiskLevel.REVERSIBLE)
    assert sm.is_blocked(RiskLevel.EXTERNAL)
    assert sm.is_blocked(RiskLevel.FINANCIAL)
    assert sm.is_blocked(RiskLevel.DESTRUCTIVE)
    # unknown/None risk is blocked (fail-closed)
    assert sm.is_blocked(None)


def test_locked_blocks_all_tool_risks(store):
    sm = SafeMode(store)
    sm.set_level(LOCKED)
    for r in (RiskLevel.READ, RiskLevel.REVERSIBLE, RiskLevel.EXTERNAL,
              RiskLevel.FINANCIAL, RiskLevel.DESTRUCTIVE):
        assert sm.is_blocked(r)
    assert sm.is_blocked(None)


def test_status_dict_shape(store):
    sm = SafeMode(store)
    sm.set_level(READONLY)
    st = sm.status_dict()
    assert st["enabled"] is True
    assert st["level"] == READONLY
    assert "policy" in st and st["policy"]


# --- engine integration ----------------------------------------------------

def _run_under(level):
    from sworker.config import WorkerConfig
    from sworker.engine import WorkerEngine
    from sworker.inference import NullInference
    from sworker.models import Run, RunStatus

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "company"), exist_ok=True)
    # seed a data file so the deterministic fallback planner emits a
    # data.query (READ) + fs.write (REVERSIBLE) step pair.
    with open(os.path.join(d, "company", "example.csv"), "w") as fh:
        fh.write("channel,revenue\nonline,100\nretail,200\n")
    store = WorkerStore(os.path.join(d, ".state"))
    sm = SafeMode(store)
    if level == OFF:
        sm.disable()
    elif level == READONLY:
        sm.set_level(READONLY)
    elif level == LOCKED:
        sm.set_level(LOCKED)

    cfg = WorkerConfig(name="w", workspace=d)
    # limit to real built-in tools the deterministic fallback planner uses
    cfg.tools = ["fs.list", "fs.write", "data.query", "knowledge.search"]
    eng = WorkerEngine(cfg, store, inference=NullInference())
    res = eng.run("produce a revenue report")
    return res, store


def test_readonly_blocks_reversible_actions():
    res, _ = _run_under(READONLY)
    # the fallback plan writes an artifact (reversible) -> must be blocked
    assert res.status.value == "BLOCKED"
    assert any("safe_mode_block" in d for d in res.run.degradations)


def test_locked_blocks_everything():
    res, _ = _run_under(LOCKED)
    assert res.status.value == "BLOCKED"
    assert any("safe_mode_block" in d for d in res.run.degradations)


def test_off_runs_normally():
    res, _ = _run_under(OFF)
    # with no company CSV data the deterministic fallback still executes its
    # steps; the key assertion is that safe mode did NOT inject a block.
    assert not any("safe_mode_block" in d for d in res.run.degradations)
