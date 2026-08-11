"""§65 — "why blocked?" explainer.

The platform already records *every* reason a run can be blocked, but the
reasons are scattered:

* ``run.error``                — incident_active / resource_exhausted / unverifiable
* ``run.degradations``         — mirroed one-liners from the degradations table
* ``degradations`` table       — category + reason + severity + mitigation
* per-step ``note``            — safe-mode block / permission deny / approval rejection
* ``incident`` ledger          — an open incident freezes new runs platform-wide

An operator triaging a BLOCKED run should not have to cross-reference four
stores by hand. :class:`BlockExplainer` aggregates them into one fail-closed
answer: a list of :class:`BlockReason` records, each with a ``source`` (where it
came from), a ``kind`` (machine tag), a human ``reason``, an optional
``mitigation``, and the ``severity``.

Fail-closed rules:

* Unknown inputs never become "not blocked". If the run record is missing or the
  status is unknown, the explainer returns ``was_blocked = None`` (explicit
  "don't know"), not ``False``.
* A ``BLOCKED`` run with *no* discoverable reason is reported with a single
  ``unknown`` block reason (severity ``critical``) rather than silently claiming
  "no blocks". Absence of a logged reason is itself a finding, not a clean bill.
* Only real data is surfaced. The explainer reads existing stores; it invents
  nothing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .models import RunStatus
from .store import WorkerStore


_UNKNOWN = "unknown"
_BLOCKED_STATUSES = {RunStatus.BLOCKED.value}
_KNOWN_STATUSES = {s.value for s in RunStatus}


class BlockReason:
    """One concrete reason a run was (or is) blocked."""

    __slots__ = ("source", "kind", "reason", "severity", "mitigation", "detail")

    def __init__(
        self,
        source: str,
        kind: str,
        reason: str,
        severity: str = "critical",
        mitigation: str = "",
        detail: str = "",
    ) -> None:
        self.source = source
        self.kind = kind
        self.reason = reason
        self.severity = severity
        self.mitigation = mitigation
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "reason": self.reason,
            "severity": self.severity,
            "mitigation": self.mitigation,
            "detail": self.detail,
        }


def _sev(rec: Dict[str, Any], fallback: str = "critical") -> str:
    s = (rec.get("severity") or "").lower()
    return s if s in ("info", "notice", "warning", "critical") else fallback


class BlockExplainer:
    """Aggregate every block signal for a run (or the whole workspace)."""

    def __init__(self, store: WorkerStore) -> None:
        self.store = store

    # -- public --------------------------------------------------------------
    def explain_run(self, run_id: str) -> Dict[str, Any]:
        """Aggregate block reasons for one run.

        Returns a dict with: ``run_id``, ``status``, ``was_blocked`` (bool|None),
        ``reasons`` (list[BlockReason.to_dict]), and ``summary`` (a one-liner).
        """
        run = self.store.get("runs", run_id)
        if not run:
            return {
                "run_id": run_id,
                "status": None,
                "was_blocked": None,
                "reasons": [
                    BlockReason("explainer", _UNKNOWN,
                                f"no run record for {run_id!r}",
                                severity="critical").to_dict()
                ],
                "summary": f"cannot explain: no run record for {run_id!r}",
            }

        status = run.get("status")
        was_blocked = status in _BLOCKED_STATUSES if status in _KNOWN_STATUSES else None
        reasons: List[BlockReason] = []

        # 1) degradations table (permission denials, safe mode, model fallback...)
        reasons.extend(self._from_degradations(run_id))
        # 2) run.error (incident_active / resource_exhausted / unverifiable)
        reasons.extend(self._from_run_error(run))
        # 3) per-step notes (safe-mode block / permission deny / approval rejection)
        reasons.extend(self._from_steps(run_id))
        # 4) platform-wide incident freeze
        reasons.extend(self._from_incident())

        # Fail-closed: a BLOCKED run with no discoverable reason is itself a flag.
        if was_blocked and not reasons:
            reasons.append(
                BlockReason(
                    "explainer", _UNKNOWN,
                    "run is BLOCKED but no block reason was recorded",
                    severity="critical",
                    mitigation="inspect the audit log: sworker audit <run_id>",
                )
            )

        return {
            "run_id": run_id,
            "status": status,
            "was_blocked": was_blocked,
            "reasons": [r.to_dict() for r in reasons],
            "summary": self._summarize(was_blocked, reasons),
        }

    def explain_workspace(self) -> Dict[str, Any]:
        """Aggregate platform-level block signals (no single run)."""
        reasons: List[BlockReason] = []
        reasons.extend(self._from_incident())
        reasons.extend(self._from_degradations(""))
        return {
            "was_blocked": bool(reasons),
            "reasons": [r.to_dict() for r in reasons],
            "summary": self._summarize(bool(reasons), reasons),
        }

    # -- sources -------------------------------------------------------------
    def _from_degradations(self, run_id: str) -> List[BlockReason]:
        out: List[BlockReason] = []
        try:
            rows = self.store.find("degradations", run_id=run_id, order="created")
        except Exception:
            return out
        for d in rows:
            sev = _sev(d, fallback="critical")
            out.append(
                BlockReason(
                    source="degradations",
                    kind=str(d.get("category") or _UNKNOWN),
                    reason=str(d.get("reason") or "(no reason recorded)"),
                    severity=sev,
                    mitigation=str(d.get("mitigation") or ""),
                )
            )
        return out

    def _from_run_error(self, run: Dict[str, Any]) -> List[BlockReason]:
        err = run.get("error") or ""
        if not err:
            return []
        # Map the known error tokens to a human reason + mitigation.
        if err == "incident_active":
            return [BlockReason(
                "run.error", "incident_active",
                "an incident is open; new runs are refused until it is closed",
                severity="critical",
                mitigation="sworker incident close",
            )]
        if err == "resource_exhausted":
            return [BlockReason(
                "run.error", "resource_exhausted",
                "a resource budget was exhausted (runtime/actions/tool calls/network)",
                severity="critical",
                mitigation="raise the limit in the worker config or split the task",
            )]
        if "verification" in err.lower() or err == "unverifiable":
            return [BlockReason(
                "run.error", "unverifiable",
                err,
                severity="warning",
                mitigation="re-run with the missing evidence or expected values",
            )]
        return [BlockReason(
            "run.error", _UNKNOWN, str(err), severity="critical"
        )]

    def _from_steps(self, run_id: str) -> List[BlockReason]:
        out: List[BlockReason] = []
        try:
            steps = self.store.find("steps", run_id=run_id, order="idx")
        except Exception:
            return out
        for s in steps:
            st = str(s.get("status") or "")
            note = (s.get("note") or "").strip()
            if st != "BLOCKED" or not note:
                continue
            kind = _UNKNOWN
            lower = note.lower()
            if "safe mode" in lower or "safemode" in lower:
                kind = "safe_mode_block"
            elif "denied" in lower or "permission" in lower:
                kind = "permission_denied"
            elif "reject" in lower or "approval" in lower:
                kind = "approval_rejected"
            sev = "critical" if kind in ("safe_mode_block", "permission_denied") else "warning"
            out.append(BlockReason(
                source="steps",
                kind=kind,
                reason=note,
                severity=sev,
                detail=str(s.get("description") or s.get("tool") or ""),
            ))
        return out

    def _from_incident(self) -> List[BlockReason]:
        try:
            from .incident import IncidentLedger
            inc = IncidentLedger(self.store)
            if not inc.active():
                return []
            sd = inc.status_dict()
            return [BlockReason(
                "incident", "incident_active",
                "an incident is open; the platform is frozen (no new runs)",
                severity="critical",
                mitigation=f"sworker incident close  (then sworker safemode off to stand the platform back down; current safe mode: {sd.get('safe_mode')})",
                detail=json.dumps(sd, default=str),
            )]
        except Exception:
            return []

    # -- formatting ----------------------------------------------------------
    @staticmethod
    def _summarize(was_blocked: Optional[bool], reasons: List[BlockReason]) -> str:
        if was_blocked is None:
            return "cannot determine block state (missing run record)"
        if not was_blocked:
            return "run is not in a BLOCKED state"
        if not reasons:
            return "BLOCKED (no recorded reason)"
        crit = [r for r in reasons if r.severity == "critical"]
        if crit:
            return f"BLOCKED: {len(crit)} critical + {len(reasons) - len(crit)} other reason(s)"
        return f"BLOCKED: {len(reasons)} reason(s)"


def explain_blocked(store: WorkerStore, run_id: str) -> Dict[str, Any]:
    """Convenience entry point used by CLI + web."""
    return BlockExplainer(store).explain_run(run_id)
