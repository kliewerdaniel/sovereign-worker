"""§63 — Incident response.

When an operator suspects the platform is in the middle of a security incident
they need a single, auditable control surface that:

* declares the incident (who, when, why);
* freezes execution — an open incident engages safe mode ``locked`` so no new
  tool action can run (built on §62);
* refuses to start *new* runs while an incident is open (fail-closed: don't
  launch fresh work into a platform you don't trust);
* records every transition in the append-only, hash-chained audit log so the
  incident timeline is tamper-evident;
* never *silently* clears itself — closing an incident does NOT auto-disable
  safe mode; the operator must explicitly stand the platform back down.

Fail-closed state
----------------
Only one incident may be open at a time. The open/closed state is persisted in
``meta_kv`` (scope ``"incident"``) and is tenant-scoped. A corrupted or
unrecognised persisted state is read back as *open + locked* — a bad value can
only ever increase restriction, never silently hide an active incident.

This module is pure stdlib; nothing here imports a third-party package.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from .degradation import CRITICAL, INCIDENT_ACTIVE
from .safemode import LOCKED, SafeMode


# Incident persistence keys live under this scope in meta_kv.
SCOPE = "incident"
K_STATE = "state"

# Persisted state values.
OPEN = "open"
CLOSED = "closed"
_STATES = (OPEN, CLOSED)

# Canonical audit event names.
E_OPENED = "incident.opened"
E_CLOSED = "incident.closed"
E_REJECTED = "incident.rejected"
E_LOCKDOWN = "incident.lockdown"

# The safe-mode level an open incident engages.
_INCIDENT_LOCK_LEVEL = LOCKED


class IncidentLedger:
    """Workspace-scoped incident controller, persisted in ``meta_kv`` and
    mirrored into the append-only audit log."""

    def __init__(self, store, scope: str = SCOPE):
        self.store = store
        self.scope = scope
        self._safe = SafeMode(store)

    # -- persistence -------------------------------------------------------
    def _key(self) -> str:
        return f"incident:state:{self.scope}"

    def _row(self) -> Optional[Dict[str, object]]:
        return self.store.get("meta_kv", self._key())

    def _read_state(self) -> str:
        row = self._row()
        st = (row or {}).get("state", CLOSED) if row else CLOSED
        # fail-closed: an unknown persisted state means "treat as open".
        return st if st in _STATES else OPEN

    def _persist(self, state: str, by: str, note: str) -> None:
        self.store.put(
            "meta_kv",
            {"id": self._key(), "scope": self.scope, "state": state,
             "by": by, "note": note, "ts": time.time()},
            event="incident.state",
        )

    # -- queries -----------------------------------------------------------
    def active(self) -> bool:
        """True if an incident is currently open. Fail-closed on corruption."""
        return self._read_state() == OPEN

    def status_dict(self) -> Dict[str, object]:
        open_ = self.active()
        return {
            "active": open_,
            "state": (OPEN if open_ else CLOSED),
            "scope": self.scope,
            "safe_mode": self._safe.level(),
            "policy": (
                "platform frozen; no new runs; safe mode "
                f"{self._safe.level()}"
                if open_
                else "no active incident"
            ),
        }

    def list_incidents(self) -> List[Dict[str, object]]:
        """Replay the incident timeline from the tamper-evident audit log."""
        out: List[Dict[str, object]] = []
        for rec in self.store.iter_audit():
            ev = rec.get("event", "")
            if ev in (E_OPENED, E_CLOSED, E_REJECTED, E_LOCKDOWN):
                p = rec.get("payload") or {}
                out.append(
                    {
                        "event": ev,
                        "ts": rec.get("ts"),
                        "by": p.get("by"),
                        "summary": p.get("summary"),
                        "note": p.get("note"),
                        "incident_id": p.get("incident_id"),
                    }
                )
        return out

    # -- transitions -------------------------------------------------------
    def open(self, summary: str, by: str = "operator", *, lock: bool = True,
             incident_id: Optional[str] = None) -> Dict[str, object]:
        """Open a new incident. Engages safe mode ``locked`` by default so no
        new tool action can run while the incident is live.

        Fail-closed: if an incident is already open, the new open is *rejected*
        (recorded) rather than silently superseding the live one.
        """
        if self.active():
            self.store.audit(
                E_REJECTED, "meta_kv", self._key(),
                {"by": by, "summary": summary, "reason":
                 "an incident is already open; close it before opening another"},
            )
            raise ValueError(
                "an incident is already open; close it (sworker incident close) "
                "before opening another"
            )
        iid = incident_id or f"inc_{int(time.time() * 1000)}"
        if lock:
            self._safe.lock()
        self._persist(OPEN, by, summary)
        self.store.audit(
            E_OPENED, "meta_kv", self._key(),
            {"by": by, "summary": summary, "incident_id": iid,
             "safe_mode": self._safe.level()},
        )
        return {"incident_id": iid, "state": OPEN, "safe_mode": self._safe.level()}

    def lockdown(self, summary: str, by: str = "operator") -> Dict[str, object]:
        """The 'freeze everything now' button: declare an incident (if not
        already open) AND force safe mode to ``locked``. Idempotent — a second
        call just re-asserts the lock and records it."""
        if not self.active():
            res = self.open(summary, by=by, lock=True)
            iid = res["incident_id"]
        else:
            iid = ""
            self._safe.lock()
        self.store.audit(
            E_LOCKDOWN, "meta_kv", self._key(),
            {"by": by, "summary": summary, "incident_id": iid,
             "safe_mode": self._safe.level()},
        )
        return {"incident_id": iid, "state": OPEN, "safe_mode": self._safe.level()}

    def close(self, by: str = "operator", note: str = "") -> Dict[str, object]:
        """Close the active incident. Records the closure in the audit log but
        does NOT auto-clear safe mode — the operator must explicitly stand the
        platform back down (``sworker safemode off``)."""
        if not self.active():
            return {"state": CLOSED, "changed": False, "safe_mode": self._safe.level()}
        self._persist(CLOSED, by, note)
        self.store.audit(
            E_CLOSED, "meta_kv", self._key(),
            {"by": by, "note": note, "safe_mode": self._safe.level()},
        )
        return {"state": CLOSED, "changed": True, "safe_mode": self._safe.level()}
