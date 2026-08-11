"""§63 — incident response tests.

These exercise the fail-closed incident ledger: an open incident engages safe
mode `locked`, refuses new runs (engine reports BLOCKED with an
`incident_active` critical degradation), only one incident may be open at a
time, closing does NOT auto-clear safe mode, and a corrupted persisted state is
read back as open (fail-closed).
"""

import os
import tempfile

from sworker.degradation import INCIDENT_ACTIVE, CRITICAL
from sworker.incident import IncidentLedger, CLOSED, OPEN
from sworker.safemode import LOCKED, SafeMode
from sworker.store import WorkerStore
from sworker.config import WorkerConfig
from sworker.engine import WorkerEngine
from sworker.inference import NullInference


def _store(d):
    os.makedirs(os.path.join(d, ".state"), exist_ok=True)
    return WorkerStore(os.path.join(d, ".state"))


def _run_under_incident(level_open=True):
    """Open an incident (engaging safe-mode locked) and run a worker that would
    otherwise write an artifact. Returns (store, result)."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "company"))
    with open(os.path.join(d, "company", "example.csv"), "w") as fh:
        fh.write("channel,revenue\nonline,100\nretail,200\n")
    store = _store(d)
    if level_open:
        IncidentLedger(store).open("test incident", by="operator")
    cfg = WorkerConfig(
        name="w",
        workspace=d,
        tools=["fs.list", "fs.write", "data.query", "knowledge.search"],
    )
    eng = WorkerEngine(cfg, store, inference=NullInference())
    res = eng.run("summarize the company")
    return store, res


def test_open_engages_locked():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    assert not led.active()
    led.open("intrusion suspected", by="op")
    assert led.active()
    assert SafeMode(store).level() == LOCKED


def test_open_rejects_second_incident():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    led.open("first", by="op")
    try:
        led.open("second", by="op")
        raise AssertionError("second open should be rejected")
    except ValueError:
        pass
    # timeline records both the open and the rejection
    kinds = [e["event"] for e in led.list_incidents()]
    assert "incident.opened" in kinds
    assert "incident.rejected" in kinds


def test_lockdown_idempotent_and_locks():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    r1 = led.lockdown("freeze", by="op")
    assert r1["safe_mode"] == LOCKED
    r2 = led.lockdown("freeze again", by="op")
    assert r2["safe_mode"] == LOCKED
    assert led.active()


def test_close_does_not_auto_clear_safemode():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    led.open("inc", by="op")
    assert SafeMode(store).level() == LOCKED
    res = led.close(by="op", note="resolved")
    assert res["changed"] is True
    assert not led.active()
    # safe mode remains locked; operator must explicitly stand down
    assert SafeMode(store).level() == LOCKED


def test_close_noop_when_inactive():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    res = led.close(by="op")
    assert res["changed"] is False


def test_new_run_refused_while_incident_open():
    _, res = _run_under_incident(level_open=True)
    assert res.status.value == "BLOCKED"
    assert any(INCIDENT_ACTIVE in d for d in res.run.degradations)


def test_run_proceeds_when_no_incident():
    store, res = _run_under_incident(level_open=False)
    # with no incident and safe mode off, the worker runs (may still FAIL on
    # verification, but must NOT be blocked by an incident)
    assert res.status.value != "BLOCKED" or "incident" not in (res.run.error or "")


def test_corrupt_state_reads_open_failclosed():
    d = tempfile.mkdtemp()
    store = _store(d)
    # write a garbage persisted state
    store.put("meta_kv", {"id": "incident:state:incident",
                          "scope": "incident", "state": "GARBAGE"},
              event="incident.state")
    led = IncidentLedger(store)
    # fail-closed: unrecognised state -> treated as open + locked
    assert led.active() is True


def test_status_dict_shape():
    d = tempfile.mkdtemp()
    store = _store(d)
    led = IncidentLedger(store)
    st = led.status_dict()
    assert set(st.keys()) >= {"active", "state", "scope", "policy"}
    assert st["active"] is False
    led.open("x", by="op")
    st2 = led.status_dict()
    assert st2["active"] is True
    assert st2["safe_mode"] == LOCKED
