"""§62 — Safe mode.

When an operator suspects the platform is in a bad state (a model that may be
misbehaving, an environment they don't trust, a security incident in progress),
they should be able to flip a single switch that makes the worker **fail closed**
instead of continuing to act in the world.

Safe mode never *silently* changes behaviour. Every block it causes is:

* **recorded** as a critical degradation in the audit log (`safe_mode_block`);
* **surfaced** on the run result (`Run.degradations`);
* **fail-closed** — if safe mode is enabled and we cannot determine whether an
  action is allowed, we block it. Safe mode is about *stopping*, not guessing.

Levels
------
* ``off``      — no effect (normal operation).
* ``readonly`` — block every action whose risk is higher than ``READ``. Read-only
                  retrieval (filesystem reads, data queries over local files,
                  knowledge search) is still permitted, because it cannot change
                  the world. Anything that writes, sends, spends, or destroys is
                  blocked.
* ``locked``   — block **every** action that would invoke a tool. The worker may
                  still plan and propose, but it executes nothing. This is the
                  "freeze the platform" switch for an active incident.

The toggle is persisted in ``meta_kv`` (``scope == "safemode"``) so it survives
restarts and is tenant-scoped per workspace store. The persisted level is
**fail-closed**: an unrecognised value on read (e.g. corrupt row) is treated as
``locked``, never as ``off`` — a bad value can only ever *increase* restriction,
never silently disable it. The only ways to change the level are explicit
operator actions (``sworker safemode <level>`` or the admin ``/api/v1/safemode``
POST); nothing in the runtime auto-downgrades it.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import RiskLevel

# Safe-mode persistence keys live under this scope in meta_kv.
SCOPE = "safemode"
K_LEVEL = "level"

# Persisted level values.
OFF = "off"
READONLY = "readonly"
LOCKED = "locked"

LEVELS = (OFF, READONLY, LOCKED)

# Risk levels that are considered "read-only" and therefore still permitted under
# the ``readonly`` level. Anything at or above REVERSIBLE mutates the world.
_READ_RISKS = {RiskLevel.READ}

# The canonical category written to the degradation ledger when safe mode blocks.
SAFE_MODE_BLOCK = "safe_mode_block"


class SafeMode:
    """Workspace-scoped safe-mode controller, persisted in ``meta_kv``."""

    def __init__(self, store, scope: str = SCOPE):
        self.store = store
        self.scope = scope

    # -- persistence -------------------------------------------------------
    def _row(self) -> Optional[Dict[str, object]]:
        return self.store.get("meta_kv", self._key())

    def _key(self) -> str:
        return f"safemode:level:{self.scope}"

    def level(self) -> str:
        row = self._row()
        lv = (row or {}).get("level", OFF) if row else OFF
        # fail-closed: an unknown persisted level must not disable the guard.
        return lv if lv in LEVELS else LOCKED

    def enabled(self) -> bool:
        return self.level() != OFF

    def set_level(self, level: str) -> str:
        if level not in LEVELS:
            raise ValueError(f"unknown safe-mode level {level!r}; valid: {LEVELS}")
        lv = level if level in LEVELS else LOCKED
        self.store.put(
            "meta_kv",
            {"id": self._key(), "scope": self.scope, "level": lv},
            event="safemode.changed",
        )
        return lv

    def enable(self) -> str:
        # default to readonly when no level is specified — the least-surprising
        # "make it stop acting" posture is read-only, not frozen.
        return self.set_level(READONLY)

    def disable(self) -> str:
        return self.set_level(OFF)

    def lock(self) -> str:
        return self.set_level(LOCKED)

    # -- decision ----------------------------------------------------------
    def is_blocked(self, risk: Optional[RiskLevel]) -> bool:
        """Return True if an action of ``risk`` must NOT execute under the
        current safe-mode level. Fail-closed: an unspecified risk is blocked,
        and a locked workspace blocks every action that would invoke a tool."""
        lv = self.level()
        if lv == OFF:
            return False
        if lv == LOCKED:
            # Frozen: every tool action is blocked, including any action whose
            # risk we cannot determine (None) — never guess during an incident.
            return True
        # readonly: block anything above READ. An unknown/None risk is blocked.
        if risk is None:
            return True
        return risk not in _READ_RISKS

    def reason(self, risk: Optional[RiskLevel]) -> str:
        lv = self.level()
        if lv == OFF:
            return ""
        if lv == LOCKED:
            return "safe mode LOCKED: execution frozen; no tool actions permitted during incident"
        return (
            f"safe mode READONLY: actions above {RiskLevel.READ.value} risk are "
            f"blocked (this action is {risk.value if risk else 'unknown'} risk)"
        )

    # -- reporting ---------------------------------------------------------
    def status_dict(self) -> Dict[str, object]:
        lv = self.level()
        return {
            "enabled": lv != OFF,
            "level": lv,
            "scope": self.scope,
            "policy": (
                "no restriction"
                if lv == OFF
                else ("read-only actions only" if lv == READONLY else "execution frozen")
            ),
        }
