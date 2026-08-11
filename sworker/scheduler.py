"""Scheduled workflows.

A Schedule is a durable record that, at its next run time, triggers an EXISTING
procedure on a worker. The scheduler is intentionally dumb: it does not invent
work, it only re-runs what a human already encoded as a procedure. All times are
local; this is a single-machine platform.

The runner is idempotent and append-only: each fire is recorded so we never
double-run and never lose the fact that a run happened.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import WorkerConfig, Workspace, default_workspace
from .models import new_id, now
from .procedures import load_procedure, procedure_steps, procedure_verifications
from .store import WorkerStore


@dataclass
class Schedule:
    worker: str
    procedure: str
    cron: str
    id: str = field(default_factory=lambda: new_id("sched"))
    enabled: bool = True
    created: float = field(default_factory=now)
    created_by: str = ""
    next_run: float = 0.0
    last_run: float = 0.0
    last_status: str = ""
    last_fired_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "worker": self.worker,
            "procedure": self.procedure,
            "cron": self.cron,
            "enabled": self.enabled,
            "created": self.created,
            "created_by": self.created_by,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_fired_by": self.last_fired_by,
        }


# -- cron parsing (stdlib only) --------------------------------------------

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}


def _field(value: str, low: int, high: int) -> List[int]:
    if value == "*":
        return list(range(low, high + 1))
    out: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if base == "*":
                out.extend(range(low, high + 1, step))
            else:
                a, b = (base.split("-") + [base])[:2]
                out.extend(range(int(a), int(b) + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def parse_cron(expr: str) -> Dict[str, List[int]]:
    expr = _ALIASES.get(expr.strip(), expr.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
    minute, hour, dom, month, dow = parts
    return {
        "minute": _field(minute, 0, 59),
        "hour": _field(hour, 0, 23),
        "dom": _field(dom, 1, 31),
        "month": _field(month, 1, 12),
        "dow": _field(dow, 0, 6),
    }


def next_fire(cron: str, after: Optional[float] = None) -> float:
    """Next epoch second (local) strictly after ``after`` matching the cron.

    Standard Vixie-cron field semantics: when BOTH day-of-month and day-of-week
    are restricted (neither is '*'), they are OR-ed; when either is '*' that
    field is ignored. This is the only subtlety; everything else is a plain set
    membership test.

    Cron day-of-week numbering is 0=Sunday..6=Saturday, whereas Python's
    ``tm_wday`` is 0=Monday..6=Sunday, so we translate before comparing.
    """
    expr = _ALIASES.get(cron.strip(), cron.strip())
    fields = parse_cron(expr)
    cron_parts = expr.split()
    dom_star = cron_parts[2].strip() == "*"
    dow_star = cron_parts[4].strip() == "*"
    # translate cron dow (0=Sun..6=Sat) to Python tm_wday (0=Mon..6=Sun)
    dow_set = set((d - 1) % 7 for d in fields["dow"])
    start = time.mktime(time.localtime((after or time.time()) + 60))
    for _ in range(5 * 366 * 24 * 60):  # bound: ~5 years of minutes
        tt = time.localtime(start)
        minute_ok = tt.tm_min in fields["minute"]
        hour_ok = tt.tm_hour in fields["hour"]
        month_ok = tt.tm_mon in fields["month"]
        if not (minute_ok and hour_ok and month_ok):
            start += 60
            continue
        dom_ok = dom_star or tt.tm_mday in fields["dom"]
        dow_ok = dow_star or tt.tm_wday in dow_set
        if dom_ok and dow_ok:
            return start
        start += 60
    raise ValueError(f"no fire time within bound for {cron!r}")


# -- persistence ------------------------------------------------------------


def add_schedule(
    store: WorkerStore,
    worker: str,
    procedure: str,
    cron: str,
    *,
    enabled: bool = True,
    created_by: str = "",
) -> Schedule:
    parse_cron(cron)  # validate up front
    nxt = next_fire(cron)
    sched = Schedule(
        worker=worker, procedure=procedure, cron=cron, enabled=enabled, next_run=nxt,
        created_by=created_by,
    )
    store.put("schedules", sched.to_dict(), event="schedule.created")
    return sched


def list_schedules(store: WorkerStore, worker: str = "") -> List[Dict[str, Any]]:
    if worker:
        return store.find("schedules", worker=worker, order="created")
    return store.find("schedules", order="created")


def get_schedule(store: WorkerStore, sched_id: str) -> Optional[Dict[str, Any]]:
    return store.get("schedules", sched_id)


def set_enabled(store: WorkerStore, sched_id: str, enabled: bool, by: str = "") -> None:
    rec = store.get("schedules", sched_id)
    if not rec:
        raise KeyError(sched_id)
    rec["enabled"] = bool(enabled)
    if enabled:
        rec["next_run"] = next_fire(rec["cron"])
    store.put("schedules", rec, event="schedule.updated")


def remove_schedule(store: WorkerStore, sched_id: str, by: str = "") -> None:
    rec = store.get("schedules", sched_id)
    if not rec:
        raise KeyError(sched_id)
    rec["enabled"] = False
    rec["next_run"] = 0.0
    store.put("schedules", rec, event="schedule.removed")


def due(store: WorkerStore, window: float = 0.0) -> List[Dict[str, Any]]:
    """Schedules whose next_run is <= now + window and enabled."""
    now_s = time.time()
    out = []
    for s in store.find("schedules", order="next_run"):
        if s["enabled"] and s["next_run"] and s["next_run"] <= now_s + window:
            out.append(s)
    return out


def mark_fired(store: WorkerStore, sched_id: str, status: str, by: str = "") -> None:
    rec = store.get("schedules", sched_id)
    if not rec:
        return
    rec["last_run"] = time.time()
    rec["last_status"] = status
    rec["last_fired_by"] = by
    rec["next_run"] = next_fire(rec["cron"], after=rec["last_run"])
    store.put("schedules", rec, event="schedule.fired")
