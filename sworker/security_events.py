"""§64 — Security events + dashboard.

The platform already writes every security-relevant transition to the
append-only, hash-chained audit log (§13): incidents (§63), safe-mode changes
(§62), permission denials, auth/session lifecycle, approval escalations, policy
and secret changes, graceful-degradation records, and fail-closed run
transitions. That raw stream is complete but noisy.

§64 curates it: a fixed catalog maps audit ``event`` names to a severity and a
human label, so an operator gets a focused, queryable **security event feed**
rather than the firehose. The raw audit log remains the source of truth
(tamper-evident); the catalog is a lens over it. Unknown events are simply not
surfaced as security events — they stay in the raw log, so adding a new audited
action never silently loses history.

Fail-closed: the feed refuses to *invent* events. It only ever reports what the
audit log actually contains. The catalog is an allow-list; anything not in it is
omitted. The dashboard also surfaces the audit-chain integrity verdict
(``verify_audit_chain``) so a tampered log is visible, not hidden.

This module is pure stdlib; nothing here imports a third-party package.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

# Severity tiers (least -> most serious). Mirrors the degradation severity
# vocabulary but is about *events*, not *capability loss*.
INFO = "info"
NOTICE = "notice"
WARNING = "warning"
CRITICAL = "critical"

# kind -> set of audited event names this catalog treats as security events.
_EVENT_CATALOG: Dict[str, Dict[str, str]] = {
    # kind: { event_name: severity }
    "incident": {
        "incident.opened": CRITICAL,
        "incident.lockdown": CRITICAL,
        "incident.rejected": WARNING,
        "incident.closed": NOTICE,
    },
    "safemode": {
        "safemode.changed": WARNING,
    },
    "permission": {
        "action.denied": WARNING,
        "action.cancelled": NOTICE,
    },
    "auth": {
        "user.created": NOTICE,
        "user.disabled": WARNING,
        "user.enabled": NOTICE,
        "user.password_changed": NOTICE,
        "session.created": INFO,
        "session.revoked": NOTICE,
    },
    "approval": {
        "approval.escalated": WARNING,
        "approval.voted": INFO,
    },
    "policy": {
        "policy.published": NOTICE,
        "policy.promoted": NOTICE,
    },
    "secret": {
        "secret.stored": NOTICE,
        "secret.deleted": WARNING,
    },
    "degradation": {
        "degradation.recorded": WARNING,  # may be downgraded by payload severity below
    },
    "run": {
        "run.transition": NOTICE,  # surfaced only when the target is a fail-closed state
    },
}

# Human-readable labels for each catalogued event.
_LABELS: Dict[str, str] = {
    "incident.opened": "incident opened",
    "incident.lockdown": "platform lockdown engaged",
    "incident.rejected": "incident open rejected (one at a time)",
    "incident.closed": "incident closed",
    "safemode.changed": "safe-mode level changed",
    "action.denied": "action denied (permission/safe-mode)",
    "action.cancelled": "action cancelled",
    "user.created": "user account created",
    "user.disabled": "user account disabled",
    "user.enabled": "user account enabled",
    "user.password_changed": "user password changed",
    "session.created": "session created",
    "session.revoked": "session revoked",
    "approval.escalated": "approval escalated",
    "approval.voted": "approval vote cast",
    "policy.published": "policy published",
    "policy.promoted": "policy promoted",
    "secret.stored": "secret stored",
    "secret.deleted": "secret deleted",
    "degradation.recorded": "capability degraded",
    "run.transition": "run state transition",
}

# Run transitions whose *target* state marks a fail-closed / safety event.
_FAIL_CLOSED_RUN_STATES = {"BLOCKED", "CANCELLED", "DENIED"}


def _event_kind(event: str) -> Optional[str]:
    for kind, events in _EVENT_CATALOG.items():
        if event in events:
            return kind
    return None


class SecurityEvents:
    """A curated, queryable view over the workspace's audit log."""

    def __init__(self, store):
        self.store = store

    # -- chain integrity (fail-closed: a broken chain is reported, not hidden) -
    def chain_ok(self) -> bool:
        try:
            return bool(self.store.verify_audit_chain().get("ok"))
        except Exception:
            return False

    def _severity_for(self, event: str, payload: Dict[str, Any]) -> str:
        kind = _event_kind(event)
        if kind is None:
            return ""
        base = _EVENT_CATALOG[kind][event]
        # a degradation record carries its own severity; honour it.
        if event == "degradation.recorded":
            sev = (payload or {}).get("severity", base)
            return sev if sev in (INFO, NOTICE, WARNING, CRITICAL) else base
        # a run transition is only a security event when it lands on a
        # fail-closed state; otherwise it is ordinary lifecycle noise.
        if event == "run.transition":
            to = (payload or {}).get("status") or (payload or {}).get("to")
            if to not in _FAIL_CLOSED_RUN_STATES:
                return ""  # sentinel: skip
        return base

    def iter_events(
        self,
        limit: int = 50,
        kinds: Optional[List[str]] = None,
        since: float = 0.0,
    ) -> Iterator[Dict[str, Any]]:
        want = set(kinds) if kinds else None
        out: List[Dict[str, Any]] = []
        for rec in self.store.iter_audit():
            ev = rec.get("event", "")
            kind = _event_kind(ev)
            if kind is None:
                continue
            if want and kind not in want:
                continue
            sev = self._severity_for(ev, rec.get("payload") or {})
            if not sev:
                continue
            if since and float(rec.get("ts", 0)) < since:
                continue
            out.append(
                {
                    "ts": rec.get("ts"),
                    "event": ev,
                    "kind": kind,
                    "severity": sev,
                    "label": _LABELS.get(ev, ev),
                    "actor": (rec.get("payload") or {}).get("by") or rec.get("id"),
                    "summary": self._summarize(ev, rec.get("payload") or {}),
                    "run_id": (rec.get("payload") or {}).get("run_id"),
                }
            )
        # newest first, capped
        out.sort(key=lambda e: float(e["ts"] or 0), reverse=True)
        return iter(out[:limit])

    def recent(self, limit: int = 50, kinds: Optional[List[str]] = None,
               since: float = 0.0) -> List[Dict[str, Any]]:
        return list(self.iter_events(limit=limit, kinds=kinds, since=since))

    def counts_by_kind(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for e in self.iter_events(limit=100000):
            c[e["kind"]] = c.get(e["kind"], 0) + 1
        return c

    @staticmethod
    def _summarize(event: str, payload: Dict[str, Any]) -> str:
        if event == "incident.opened":
            return f"incident opened: {payload.get('summary', '')}"
        if event == "incident.lockdown":
            return f"lockdown: {payload.get('summary', '')}"
        if event == "incident.rejected":
            return "a second incident was rejected while one is open"
        if event == "incident.closed":
            return f"incident closed: {payload.get('note', '')}"
        if event == "safemode.changed":
            return f"safe mode -> {(payload or {}).get('level', '?')}"
        if event == "action.denied":
            return f"denied: {payload.get('summary', '')}"
        if event == "degradation.recorded":
            return f"{payload.get('category', '')}: {payload.get('reason', '')}"
        if event == "run.transition":
            return f"run -> {payload.get('status', '?')}"
        if event.startswith("user.") or event.startswith("session."):
            return event
        if event.startswith("policy."):
            return f"{payload.get('scope', '')}: {event}"
        if event.startswith("secret."):
            return f"{payload.get('name', '')}: {event}"
        if event.startswith("approval."):
            return f"{payload.get('approval_id', '')}: {event}"
        return event
