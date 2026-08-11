"""§66 — Composable system-status surface.

The platform's hardening subsystems (§42-§65) each already publish a snapshot of
their own state:

* ``SafeMode``         -> level() / enabled()                (off / readonly / locked)
* ``IncidentLedger``   -> status_dict()                     (active / closed)
* ``DegradationLedger``-> any_critical() / summary()         (graceful-degradation ledger)
* ``SecurityEvents``   -> counts_by_kind() + chain verdict  (§64 feed)
* ``BlockExplainer``   -> explain_workspace()               (§65 aggregated block reasons)

Each is independently useful, but an operator still has to open five places to
answer "is this platform healthy right now?". §66 adds a *thin, uniform* layer:

* ``ControlSnapshot``  — one shape for every control's state.
* ``SystemStatus``     — composes the real snapshots into a single fail-closed
  verdict (worst severity wins), never inventing a control or a reason.

The adapters read only each subsystem's *existing* public surface; the
subsystems themselves are not modified, so their tests keep holding.

Fail-closed by construction
----------------------------
* **It only reports what the subsystems report — it invents nothing.** No
  hardcoded "you are healthy" text; the verdict is derived from the real
  control severities.
* **A control that raises is reported as ``unknown`` (not ``ok``).** One broken
  probe can never paint the platform green.
* **Verdict = worst severity present.** ``critical > unknown > warning > ok``.
  ``unknown`` outranks ``warning`` so a probe that couldn't answer can't be
  assumed benign.
* **No noise suppression.** Every control is listed, including ``ok`` ones, so a
  reader sees what was checked, not just what failed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .degradation import CRITICAL as _DEG_CRIT
from .degradation import DegradationLedger, WARN as _DEG_WARN
from .incident import IncidentLedger
from .safemode import LOCKED, OFF, READONLY, SafeMode

# Severity vocabulary (kept as constants so call sites can't typo one).
OK = "ok"
WARNING = "warning"
CRITICAL = "critical"
UNKNOWN = "unknown"

# Higher rank = more severe. ``unknown`` outranks ``warning``: a probe that
# couldn't answer must not be assumed benign (fail-closed).
_SEVERITY_RANK = {OK: 1, WARNING: 2, UNKNOWN: 3, CRITICAL: 4}


@dataclass
class ControlSnapshot:
    """One control's state, in the uniform shape ``SystemStatus`` aggregates."""

    name: str
    severity: str               # one of OK / WARNING / CRITICAL / UNKNOWN
    status: str                 # human-facing one-liner (real data, not invented)
    source: str                 # module.symbol that produced this snapshot
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "status": self.status,
            "source": self.source,
            "detail": self.detail,
        }


def _rank(sev: str) -> int:
    return _SEVERITY_RANK.get(sev, _SEVERITY_RANK[UNKNOWN])


# --- adapters: each reads an existing subsystem's public surface ------------

def snapshot_safemode(store) -> ControlSnapshot:
    sm = SafeMode(store)
    level = sm.level()
    if level == LOCKED:
        severity = CRITICAL
        status = "safe mode LOCKED — every tool action is blocked"
    elif level == READONLY:
        severity = WARNING
        status = "safe mode READONLY — writes/fork/exec above READ risk are blocked"
    elif level == OFF:
        severity = OK
        status = "safe mode OFF"
    else:
        # an unrecognized level is fail-closed to LOCKED by SafeMode.level();
        # surface it as critical rather than trusting "off".
        severity = CRITICAL
        status = f"safe mode at unrecognized level '{level}' (treated as locked)"
    return ControlSnapshot(
        name="safe_mode", severity=severity, status=status,
        source="sworker.safemode.SafeMode.level",
        detail={"level": level, "enabled": sm.enabled()},
    )


def snapshot_incident(store) -> ControlSnapshot:
    led = IncidentLedger(store)
    d = led.status_dict()
    active = bool(d.get("active"))
    return ControlSnapshot(
        name="incident",
        severity=CRITICAL if active else OK,
        status=(
            "an incident is OPEN — the platform is frozen (no new runs)"
            if active else "no active incident"
        ),
        source="sworker.incident.IncidentLedger.status_dict",
        detail=d,
    )


def snapshot_degradation(store) -> ControlSnapshot:
    deg = DegradationLedger(store)
    entries = deg.entries()
    if deg.any_critical():
        severity = CRITICAL
        status = f"{len(entries)} degradation(s) recorded; at least one is CRITICAL"
    elif entries:
        severity = WARNING
        status = f"{len(entries)} degradation(s) recorded (no critical)"
    else:
        severity = OK
        status = "no graceful-degradation events recorded"
    return ControlSnapshot(
        name="degradation", severity=severity, status=status,
        source="sworker.degradation.DegradationLedger",
        detail={"count": len(entries), "summary": deg.summary()},
    )


def snapshot_security(store) -> ControlSnapshot:
    from .security_events import SecurityEvents

    sec = SecurityEvents(store)
    chain = store.verify_audit_chain()
    ok = bool(chain.get("ok"))
    counts = sec.counts_by_kind()
    if not ok:
        severity = CRITICAL
        status = "audit chain BROKEN — security log integrity cannot be trusted"
    elif counts:
        severity = WARNING
        status = "security events recorded (audit chain intact)"
    else:
        severity = OK
        status = "no security events recorded; audit chain intact"
    return ControlSnapshot(
        name="security", severity=severity, status=status,
        source="sworker.security_events.SecurityEvents",
        detail={
            "audit_chain_ok": ok,
            "audit_checked": chain.get("checked", 0),
            "counts_by_kind": counts,
        },
    )


def snapshot_blocked(store) -> ControlSnapshot:
    from .block_explainer import BlockExplainer

    out = BlockExplainer(store).explain_workspace()
    was = out.get("was_blocked")
    reasons = out.get("reasons", [])
    crit = sum(1 for r in reasons if r.get("severity") == CRITICAL)
    if was is True:
        severity = CRITICAL if crit else WARNING
        status = f"platform/workspace is BLOCKED ({len(reasons)} reason(s))"
    elif was is False:
        severity = OK if not crit else WARNING
        status = "no active block on the workspace"
    else:
        severity = UNKNOWN
        status = "block status indeterminate (no run records / unknown status)"
    return ControlSnapshot(
        name="blocked", severity=severity, status=status,
        source="sworker.block_explainer.BlockExplainer.explain_workspace",
        detail={"was_blocked": was, "reason_count": len(reasons)},
    )


# The registry is just an ordered list of adapter callables. Adding a new
# hardening subsystem later = appending one adapter here; nothing else changes.
ADAPTERS: List[Callable[[Any], ControlSnapshot]] = [
    snapshot_safemode,
    snapshot_incident,
    snapshot_degradation,
    snapshot_security,
    snapshot_blocked,
]


class SystemStatus:
    """Compose every registered control into one fail-closed verdict."""

    def __init__(self, store) -> None:
        self.store = store

    def compose(self) -> Dict[str, Any]:
        snapshots: List[ControlSnapshot] = []
        for adapter in ADAPTERS:
            try:
                snap = adapter(self.store)
            except Exception as exc:  # fail-closed: a broken probe is unknown,
                snap = ControlSnapshot(  # never silently ok
                    name=getattr(adapter, "__name__", "unknown"),
                    severity=UNKNOWN,
                    status=f"control probe raised: {type(exc).__name__}: {exc}",
                    source=getattr(adapter, "__module__", "") + "."
                    + getattr(adapter, "__name__", "unknown"),
                )
            snapshots.append(snap)

        verdict = OK
        for s in snapshots:
            if _rank(s.severity) > _rank(verdict):
                verdict = s.severity
        return {
            "verdict": verdict,
            "generated_at": time.time(),
            "controls": [s.to_dict() for s in snapshots],
        }

    def verdict(self) -> str:
        return self.compose()["verdict"]
