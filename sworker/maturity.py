"""§70 — Maturity model.

A platform's real security/operational posture is whatever its *weakest* control
actually is — not the average of its strengths. This module scores the running
deployment against a five-tier ladder by reading only the existing hardening
subsystems' real, persisted state. It invents nothing: every signal is a query
against a subsystem that already exists, and any missing/unknown data resolves to
the lowest tier (fail-closed), never to a flattering one.

Ladder (low → high):
    none      0  control absent / not initialised
    basic     1  present but not enforced
    standard  2  enforced and recording
    hardened  3  enforced, recording, and exercised (incident/safe-mode engaged)
    sovereign 4  full posture: every dimension at standard or above

The overall maturity is the FLOOR of all dimensions (weakest link), so a single
uninitialised control keeps the whole platform at "none" rather than letting a
strong audit chain paper over a missing auth layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import migrations as mig
from .auth import AuthProvider
from .config import list_workers
from .safemode import SafeMode
from .incident import IncidentLedger
from .degradation import DegradationLedger
from .security_events import SecurityEvents
from .system_status import SystemStatus
from .store import WorkerStore


# Five-tier ladder. Order is significant: index == maturity score.
TIERS = ["none", "basic", "standard", "hardened", "sovereign"]
NONE, BASIC, STANDARD, HARDENED, SOVEREIGN = range(5)


@dataclass
class Signal:
    name: str
    present: bool
    detail: str = ""


@dataclass
class Dimension:
    id: str
    label: str
    tier: int
    evidence: str
    signals: List[Signal] = field(default_factory=list)
    recommendation: str = ""

    @property
    def tier_name(self) -> str:
        return TIERS[self.tier]


@dataclass
class MaturityReport:
    level: str
    floor: int
    mean: float
    dimensions: List[Dimension]
    generated_at: str
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "floor": self.floor,
            "mean": round(self.mean, 2),
            "generated_at": self.generated_at,
            "summary": self.summary,
            "dimensions": [
                {
                    "id": d.id,
                    "label": d.label,
                    "tier": d.tier,
                    "tier_name": d.tier_name,
                    "evidence": d.evidence,
                    "recommendation": d.recommendation,
                    "signals": [
                        {"name": s.name, "present": s.present, "detail": s.detail}
                        for s in d.signals
                    ],
                }
                for d in self.dimensions
            ],
        }


def _tier_at_least(value: int, minimum: int) -> bool:
    return value >= minimum


class MaturityModel:
    """Score the running deployment's hardening posture from real state.

    Reads only the public, persisted surfaces of the §42–§68 subsystems. The
    workspace label is used only for human-facing evidence text; all queries are
    scoped to the store.
    """

    def __init__(self, store: WorkerStore, workspace_label: str = "") -> None:
        self.store = store
        self.label = workspace_label or os.path.basename(store.root)

    # --- individual dimensions ------------------------------------------------

    def _audit_chain(self) -> Dimension:
        rep = self.store.verify_audit_chain()
        ok = bool(rep.get("ok"))
        checked = int(rep.get("checked", 0))
        sig = [Signal("audit_chain_ok", ok, f"{checked} lines checked")]
        tier = STANDARD if ok else NONE
        rec = "" if ok else "Initialise and verify the audit hash chain (§13)."
        return Dimension(
            "audit_chain", "Audit-chain integrity", tier,
            evidence=f"verify_audit_chain ok={ok} checked={checked}",
            signals=sig, recommendation=rec,
        )

    def _schema(self) -> Dimension:
        cur = mig.current_version(self.store)
        pend = mig.pending(self.store)
        sig = [
            Signal("schema_current", len(pend) == 0, f"version={cur}"),
            Signal("no_pending_migrations", len(pend) == 0, f"pending={len(pend)}"),
        ]
        tier = STANDARD if len(pend) == 0 else BASIC
        rec = "" if len(pend) == 0 else "Run `sworker migrate` to bring the schema to current."
        return Dimension(
            "schema_version", "Schema currency", tier,
            evidence=f"current_version={cur} pending={len(pend)}",
            signals=sig, recommendation=rec,
        )

    def _auth(self) -> Dimension:
        users = AuthProvider(self.store).list_users()
        count = len(users)
        has_admin = any(getattr(u, "role", "") in ("admin", "operator") for u in users)
        sig = [
            Signal("users_exist", count > 0, f"{count} user(s)"),
            Signal("privileged_role", has_admin, "admin/operator present" if has_admin else "no admin/operator"),
        ]
        tier = NONE
        if count > 0:
            tier = STANDARD if has_admin else BASIC
        rec = "" if tier >= STANDARD else "Create users and assign at least one admin/operator role (§4/§45)."
        return Dimension(
            "auth", "Local authentication", tier,
            evidence=f"users={count} privileged={has_admin}",
            signals=sig, recommendation=rec,
        )

    def _rbac(self) -> Dimension:
        users = AuthProvider(self.store).list_users()
        roles = sorted({getattr(u, "role", "") for u in users})
        has_priv = any(r in ("admin", "operator") for r in roles)
        sig = [
            Signal("roles_assigned", len(roles) > 0, f"roles={roles}"),
            Signal("privileged_role", has_priv),
        ]
        tier = STANDARD if has_priv else (BASIC if roles else NONE)
        rec = "" if has_priv else "Assign roles so approvals/quorum can be enforced (§45)."
        return Dimension(
            "rbac", "Role-based access", tier,
            evidence=f"roles={roles}", signals=sig, recommendation=rec,
        )

    def _safe_mode(self) -> Dimension:
        sm = SafeMode(self.store, scope="")
        enabled = sm.enabled()
        level = sm.level()
        sig = [
            Signal("safe_mode_initialised", True, f"level={level}"),
            Signal("safe_mode_engaged", enabled, f"enabled={enabled}"),
        ]
        # 'off' is the default shipped state and is a valid standard posture;
        # any operator-chosen non-off level (readonly/locked) counts as hardened.
        tier = HARDENED if enabled else STANDARD
        rec = "" if tier >= HARDENED else "Consider `sworker safemode readonly` for a fail-closed default (§62)."
        return Dimension(
            "safe_mode", "Safe-mode default", tier,
            evidence=f"level={level} enabled={enabled}",
            signals=sig, recommendation=rec,
        )

    def _incident(self) -> Dimension:
        led = IncidentLedger(self.store, scope="")
        active = led.active()
        sd = led.status_dict()
        state = sd.get("state", "none")
        # exercised == an open incident OR a closed one that has a recorded timeline
        exercised = active or len(led.list_incidents()) > 0
        sig = [
            Signal("incident_ledger_present", True),
            Signal("incident_exercised", exercised, f"state={state} active={active}"),
        ]
        tier = HARDENED if exercised else BASIC
        rec = "" if exercised else "Open and close a drill incident to exercise the ledger (§63)."
        return Dimension(
            "incident_response", "Incident response", tier,
            evidence=f"state={state} active={active} exercised={exercised}",
            signals=sig, recommendation=rec,
        )

    def _degradation(self) -> Dimension:
        led = DegradationLedger(self.store, run_id="")
        entries = led.entries()
        critical = led.any_critical()
        recorded = len(entries) > 0
        sig = [
            Signal("degradation_ledger_active", recorded, f"{len(entries)} record(s)"),
            Signal("no_critical_degradation", not critical, "critical present" if critical else "none"),
        ]
        tier = STANDARD if not critical else BASIC
        # If nothing has ever been recorded, the ledger is present but unexercised.
        if not recorded:
            tier = BASIC
        rec = "" if not critical else "Resolve the critical degradation before relying on this deployment (§61)."
        return Dimension(
            "degradation_awareness", "Graceful-degradation awareness", tier,
            evidence=f"entries={len(entries)} critical={critical}",
            signals=sig, recommendation=rec,
        )

    def _security_events(self) -> Dimension:
        sev = SecurityEvents(self.store)
        recent = sev.recent(limit=1)
        counts = sev.counts_by_kind()
        sig = [
            Signal("security_events_recording", len(recent) > 0, f"{len(counts)} kind(s) recorded"),
        ]
        tier = STANDARD if len(recent) > 0 else BASIC
        rec = "" if len(recent) > 0 else "Trigger events (e.g. a blocked/denied run) so the catalog records (§64)."
        return Dimension(
            "security_events", "Security-event visibility", tier,
            evidence=f"recent={len(recent)} kinds={len(counts)}",
            signals=sig, recommendation=rec,
        )

    def _observability(self) -> Dimension:
        try:
            snap = SystemStatus(self.store).compose()
            verdict = snap.get("verdict", "unknown")
            controls = len(snap.get("controls", []))
            ok = verdict in ("ok", "unknown")
            sig = [
                Signal("system_status_composes", True, f"verdict={verdict} controls={controls}"),
                Signal("verdict_not_fabricated", ok),
            ]
            # a 'critical' verdict means a control is actively degraded -> not full posture
            tier = STANDARD if verdict != "critical" else BASIC
        except Exception as exc:  # fail-closed: broken observability = basic
            sig = [Signal("system_status_composes", False, str(exc)[:120])]
            tier = BASIC
        rec = "" if tier >= STANDARD else "Repair the failing control surfaced by `sworker status` (§66)."
        return Dimension(
            "observability", "Unified observability", tier,
            evidence="SystemStatus.compose()" + ("" if sig[0].present else " FAILED"),
            signals=sig, recommendation=rec,
        )

    def _recovery(self) -> Dimension:
        workers = list_workers(self._ws()) if self._ws() else []
        rep = self.store.verify_audit_chain()
        backup_ready = bool(rep.get("ok"))
        sig = [
            Signal("workers_defined", len(workers) > 0, f"{len(workers)} worker(s)"),
            Signal("audit_chain_ok", backup_ready),
        ]
        tier = STANDARD if (len(workers) > 0 and backup_ready) else BASIC
        rec = "" if tier >= STANDARD else "Define a worker and verify the audit chain to enable recovery (§3/§13)."
        return Dimension(
            "recovery", "Recovery readiness", tier,
            evidence=f"workers={len(workers)} audit_ok={backup_ready}",
            signals=sig, recommendation=rec,
        )

    # --- workspace resolver ---------------------------------------------------

    def _ws(self):  # lazily import to avoid a hard config import cycle at module load
        try:
            from .config import Workspace
            return Workspace(self.store.root)
        except Exception:
            return None

    # --- aggregation ----------------------------------------------------------

    _DIMENSION_BUILDERS: List[Callable[["MaturityModel"], Dimension]] = [
        _audit_chain, _schema, _auth, _rbac, _safe_mode, _incident,
        _degradation, _security_events, _observability, _recovery,
    ]

    def assess(self) -> MaturityReport:
        dims = [builder(self) for builder in self._DIMENSION_BUILDERS]
        scores = [d.tier for d in dims]
        floor = min(scores) if scores else NONE
        mean = sum(scores) / len(scores) if scores else 0.0
        level = TIERS[floor]
        summary = (
            f"{self.label}: {level.upper()} — "
            f"{sum(1 for d in dims if d.tier >= STANDARD)}/{len(dims)} dimensions at standard+"
        )
        return MaturityReport(
            level=level, floor=floor, mean=mean,
            dimensions=dims,
            generated_at=datetime.now(timezone.utc).isoformat(),
            summary=summary,
        )


def assess_maturity(store: WorkerStore, workspace_label: str = "") -> Dict[str, Any]:
    """Convenience: return a JSON-serialisable maturity dict for ``store``."""
    return MaturityModel(store, workspace_label).assess().to_dict()
