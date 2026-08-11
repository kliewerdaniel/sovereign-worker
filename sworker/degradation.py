"""§61 — graceful degradation ledger.

The platform is built to keep running when a non-essential capability is
unavailable (no local model -> deterministic fallback plan; Hermes Atlas absent
-> plaintext grep; optional crypto absent -> secrets feature disabled). Those
degradations are *good* — they keep the worker useful instead of crashing.

What was missing: a degradation was invisible. An operator reading a run's
result had no way to tell "ran with full knowledge compilation" from "ran on a
deterministic fallback with zero model". §61 makes every degradation a
**first-class, auditable record** that is:

* recorded in the ``degradations`` store table,
* mirrored into the append-only audit log (so it is tamper-evident),
* surfaced on the ``Run`` record, and
* fail-closed: a *critical* degradation (a safety-relevant check that could not
  run) forces the run off full ``SUCCESS`` at finalize — exactly like an
  unverifiable claim.

This module is pure stdlib; nothing here imports a third-party package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .store import WorkerStore  # noqa: F401  (re-exported for callers)


# Severity levels. `warn` = degraded but runnable; `critical` = a safety /
# correctness-relevant capability was skipped and the run must not claim full
# SUCCESS.
WARN = "warn"
CRITICAL = "critical"
_SEVERITIES = (WARN, CRITICAL)

# Known categories. Kept as constants so call sites can't typo a category.
MODEL_FALLBACK = "model_fallback"          # no reachable LLM; deterministic plan
KNOWLEDGE_UNCOMPILED = "knowledge_uncompiled"  # Atlas absent; plaintext grep only
SECRETS_UNAVAILABLE = "secrets_unavailable"    # crypto missing; secrets disabled
SANDBOX_HOST = "sandbox_host"              # requested isolation unavailable (closed)
INCIDENT_ACTIVE = "incident_active"        # an incident is open; new runs refused (§63)


@dataclass
class DegradationRecord:
    """A single recorded degradation of capability during a run or at startup."""

    category: str
    reason: str
    severity: str = WARN
    mitigation: str = ""        # what the operator can do to restore full capability
    run_id: str = ""
    id: str = field(default_factory=lambda: f"deg_{_rid()}")
    created: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "reason": self.reason,
            "severity": self.severity,
            "mitigation": self.mitigation,
            "run_id": self.run_id,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DegradationRecord":
        return cls(
            category=d.get("category", ""),
            reason=d.get("reason", ""),
            severity=d.get("severity", WARN),
            mitigation=d.get("mitigation", ""),
            run_id=d.get("run_id", ""),
            id=d.get("id", f"deg_{_rid()}"),
            created=d.get("created", 0.0),
        )


def _rid() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class DegradationLedger:
    """Records degradations for a workspace (or a single run) and makes them
    queryable + fail-closed at finalize."""

    def __init__(self, store: WorkerStore, run_id: str = ""):
        self.store = store
        self.run_id = run_id
        self._entries: List[DegradationRecord] = []

    def record(
        self,
        category: str,
        reason: str,
        *,
        severity: str = WARN,
        mitigation: str = "",
        run_id: Optional[str] = None,
    ) -> DegradationRecord:
        if severity not in _SEVERITIES:
            # fail-closed: an unknown severity is treated as critical, never
            # silently downgraded to a harmless "warn".
            severity = CRITICAL
        rid = run_id if run_id is not None else self.run_id
        rec = DegradationRecord(
            category=category,
            reason=reason,
            severity=severity,
            mitigation=mitigation,
            run_id=rid,
        )
        # Persist + audit. The audit line is what makes the degradation
        # tamper-evident; the table is what makes it queryable per run.
        self.store.put("degradations", rec.to_dict(), event="degradation.recorded")
        self._entries.append(rec)
        return rec

    def entries(self, run_id: Optional[str] = None) -> List[DegradationRecord]:
        rid = run_id if run_id is not None else self.run_id
        if rid:
            rows = self.store.find("degradations", run_id=rid, order="created")
        else:
            rows = self.store.find("degradations", order="created")
        return [DegradationRecord.from_dict(r) for r in rows]

    def any_critical(self, run_id: Optional[str] = None) -> bool:
        return any(e.severity == CRITICAL for e in self.entries(run_id))

    def summary(self, run_id: Optional[str] = None) -> List[str]:
        """Human-facing one-liners: 'category: reason [severity]'."""
        return [
            f"{e.category}: {e.reason} [{e.severity}]" for e in self.entries(run_id)
        ]
