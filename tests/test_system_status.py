"""§66 — composable system-status surface tests.

The composer must aggregate the *real* control snapshots (read from the existing
subsystems) into one worst-severity-wins verdict, never invent a control or a
reason, and treat a broken probe as ``unknown`` (not ``ok``).
"""

import os
import tempfile

from sworker.store import WorkerStore
from sworker.system_status import (
    SystemStatus, ControlSnapshot, OK, WARNING, CRITICAL, UNKNOWN,
    ADAPTERS, snapshot_safemode, snapshot_incident, snapshot_degradation,
    snapshot_security, snapshot_blocked,
)
from sworker.incident import IncidentLedger
from sworker.safemode import SafeMode
from sworker.degradation import DegradationLedger, CRITICAL as DCRIT


def _store():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".state"), exist_ok=True)
    return WorkerStore(os.path.join(d, ".state"))


def test_all_registered_controls_present():
    # every adapter must be wired; names are stable contract
    names = {a.__name__ for a in ADAPTERS}
    assert {"snapshot_safemode", "snapshot_incident", "snapshot_degradation",
            "snapshot_security", "snapshot_blocked"} <= names


def test_clean_workspace_verdict_ok():
    store = _store()
    out = SystemStatus(store).compose()
    assert out["verdict"] == OK
    # every control reported (no noise suppression), all ok
    assert len(out["controls"]) == len(ADAPTERS)
    assert all(c["severity"] == OK for c in out["controls"])


def test_incident_raises_verdict_to_critical():
    store = _store()
    IncidentLedger(store).open("breach", by="op")
    out = SystemStatus(store).compose()
    assert out["verdict"] == CRITICAL
    inc = next(c for c in out["controls"] if c["name"] == "incident")
    assert inc["severity"] == CRITICAL


def test_safemode_locked_raises_verdict_to_critical():
    store = _store()
    SafeMode(store).lock()
    out = SystemStatus(store).compose()
    assert out["verdict"] == CRITICAL
    sm = next(c for c in out["controls"] if c["name"] == "safe_mode")
    assert sm["severity"] == CRITICAL


def test_degradation_critical_raises_verdict():
    store = _store()
    DegradationLedger(store, run_id="r").record(
        "knowledge_uncompiled", "atlas gone", severity=DCRIT, run_id="r")
    out = SystemStatus(store).compose()
    assert out["verdict"] == CRITICAL
    deg = next(c for c in out["controls"] if c["name"] == "degradation")
    assert deg["severity"] == CRITICAL


def test_broken_probe_is_unknown_not_ok():
    store = _store()

    def boom(s):
        raise RuntimeError("probe exploded")

    # monkeypatch one adapter to fail
    orig = list(ADAPTERS)
    try:
        ADAPTERS[:] = [boom]
        out = SystemStatus(store).compose()
        assert out["verdict"] == UNKNOWN
        assert out["controls"][0]["severity"] == UNKNOWN
        assert "probe raised" in out["controls"][0]["status"]
    finally:
        ADAPTERS[:] = orig


def test_worst_severity_wins_ranking():
    # unknown outranks warning; critical outranks unknown
    assert UNKNOWN not in (OK, WARNING)  # sanity
    from sworker.system_status import _rank
    assert _rank(CRITICAL) > _rank(UNKNOWN) > _rank(WARNING) > _rank(OK)


def test_snapshots_are_real_not_invented():
    # each adapter returns a ControlSnapshot sourced at its subsystem
    store = _store()
    snaps = {
        "safe_mode": snapshot_safemode(store),
        "incident": snapshot_incident(store),
        "degradation": snapshot_degradation(store),
        "security": snapshot_security(store),
        "blocked": snapshot_blocked(store),
    }
    for name, snap in snaps.items():
        assert isinstance(snap, ControlSnapshot)
        assert snap.name == name
        assert snap.source  # must cite the real subsystem symbol
        assert snap.status  # must carry real text, never empty
