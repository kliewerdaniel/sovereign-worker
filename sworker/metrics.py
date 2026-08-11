"""§34 observability / metrics.

A tiny, dependency-free counter registry. The engine increments counters at
well-defined lifecycle points; anything can read a snapshot for export. No
third-party deps, no network — just in-process counters that the CLI/web can
serialize.
"""

from __future__ import annotations

from typing import Dict

from .models import RunStatus


class Metrics:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self.runs_by_worker: Dict[str, int] = {}
        self.runs_by_status: Dict[str, int] = {}

    def inc(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by

    def record_run(self, worker: str, status: "RunStatus | str") -> None:
        self.inc("runs_total")
        self.runs_by_worker[worker] = self.runs_by_worker.get(worker, 0) + 1
        key = str(status)
        self.runs_by_status[key] = self.runs_by_status.get(key, 0) + 1
        if key in (RunStatus.SUCCESS.value, RunStatus.PARTIAL_SUCCESS.value):
            self.inc("runs_success")
        elif key in (RunStatus.FAILED.value, RunStatus.BLOCKED.value):
            self.inc("runs_failed")

    def record_action(self, outcome: str) -> None:
        # outcome in {executed, denied, awaited}
        self.inc(f"actions_{outcome}")

    def record_approval(self, outcome: str) -> None:
        self.inc(f"approvals_{outcome}")

    def snapshot(self) -> Dict[str, object]:
        return {
            "counters": dict(self.counters),
            "runs_by_worker": dict(self.runs_by_worker),
            "runs_by_status": dict(self.runs_by_status),
        }


# Module-level default registry, shared by the running process.
DEFAULT = Metrics()


def record_run(worker: str, status: "RunStatus | str") -> None:
    DEFAULT.record_run(worker, status)


def record_action(outcome: str) -> None:
    DEFAULT.record_action(outcome)


def record_approval(outcome: str) -> None:
    DEFAULT.record_approval(outcome)


def snapshot() -> Dict[str, object]:
    return DEFAULT.snapshot()
