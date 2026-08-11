"""§64 — security events tests.

These exercise the curated security-event feed: it surfaces the security-relevant
subset of the append-only audit log with severity + kind, honours a degradation
record's own severity, only treats a run.transition as a security event when it
lands on a fail-closed state, reports the audit-chain integrity verdict, filters
by kind, and never *invents* events (only what the log actually contains).
"""

import os
import tempfile

from sworker.store import WorkerStore
from sworker.security_events import SecurityEvents, CRITICAL, WARNING, NOTICE
from sworker.incident import IncidentLedger
from sworker.safemode import SafeMode


def _store():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".state"), exist_ok=True)
    return WorkerStore(os.path.join(d, ".state"))


def test_surfaces_incident_events():
    store = _store()
    IncidentLedger(store).open("intrusion", by="op")
    IncidentLedger(store).close(by="op", note="done")
    sec = SecurityEvents(store)
    kinds = sec.counts_by_kind()
    assert kinds.get("incident", 0) >= 1
    labels = [e["label"] for e in sec.recent()]
    assert any("incident opened" in l for l in labels)
    assert any("incident closed" in l for l in labels)


def test_safemode_change_is_security_event():
    store = _store()
    SafeMode(store).lock()
    sec = SecurityEvents(store)
    by_kind = sec.counts_by_kind()
    assert by_kind.get("safemode", 0) == 1
    ev = [e for e in sec.recent() if e["kind"] == "safemode"][0]
    assert ev["severity"] == WARNING


def test_degradation_severity_honoured():
    store = _store()
    # record a critical degradation directly through the ledger API
    from sworker.degradation import DegradationLedger
    DegradationLedger(store).record("model_fallback", "no model",
                                     severity=CRITICAL, run_id="r1")
    sec = SecurityEvents(store)
    evs = [e for e in sec.recent() if e["event"] == "degradation.recorded"]
    assert evs, "degradation should be surfaced"
    assert evs[0]["severity"] == CRITICAL


def test_run_transition_only_when_fail_closed():
    store = _store()
    # a normal transition event to SUCCESS -> not a security event
    store.audit("run.transition", "runs", "run_x",
                {"status": "SUCCESS", "from": "VERIFYING"})
    # a fail-closed transition to BLOCKED -> IS a security event
    store.audit("run.transition", "runs", "run_y",
                {"status": "BLOCKED", "from": "PLANNING"})
    sec = SecurityEvents(store)
    transitions = [e for e in sec.recent() if e["event"] == "run.transition"]
    assert len(transitions) == 1
    assert transitions[0]["summary"] == "run -> BLOCKED"


def test_chain_integrity_reported():
    store = _store()
    store.audit("incident.state", "meta_kv", "incident:state:incident",
                {"state": "open"})
    sec = SecurityEvents(store)
    # a fresh store with only clean hashed lines verifies OK
    assert sec.chain_ok() is True
    payload_ok = store.verify_audit_chain().get("ok")
    assert payload_ok is True


def test_kind_filter():
    store = _store()
    IncidentLedger(store).open("x", by="op")
    SafeMode(store).lock()
    sec = SecurityEvents(store)
    only = sec.recent(kinds=["incident"])
    assert only, "should return incident events"
    assert all(e["kind"] == "incident" for e in only)
    none = sec.recent(kinds=["auth"])
    assert none == []  # no auth events were emitted


def test_does_not_invent_events():
    store = _store()
    # emit only an incident; no permission/auth/etc events should appear
    IncidentLedger(store).open("x", by="op")
    sec = SecurityEvents(store)
    events = sec.recent()
    # every surfaced event must correspond to a real catalogued audit event
    for e in events:
        assert e["event"] in (
            "incident.opened", "incident.lockdown", "incident.rejected",
            "incident.closed", "safemode.changed", "action.denied",
            "action.cancelled", "user.created", "user.disabled",
            "user.enabled", "user.password_changed", "session.created",
            "session.revoked", "approval.escalated", "approval.voted",
            "policy.published", "policy.promoted", "secret.stored",
            "secret.deleted", "degradation.recorded", "run.transition",
        )


def test_severity_levels_present():
    store = _store()
    # incident.opened is critical
    IncidentLedger(store).open("x", by="op")
    sec = SecurityEvents(store)
    ev = [e for e in sec.recent() if e["event"] == "incident.opened"][0]
    assert ev["severity"] == CRITICAL
    # closing is notice
    IncidentLedger(store).close(by="op")
    ev2 = [e for e in sec.recent() if e["event"] == "incident.closed"][0]
    assert ev2["severity"] == NOTICE
