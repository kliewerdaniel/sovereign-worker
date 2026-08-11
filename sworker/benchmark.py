"""§58/§59 — regression & performance benchmarks.

These are *real* measurements of the deterministic engine path (no language
model, no cloud). They are deterministic by construction: the fallback planner
and the data tools do the same work every time, so a regression that blows up
planning/execution latency or that silently changes the answer is caught.

Design rules (match the rest of the platform):

* **Fail-closed, never fabricated.** A measurement is only emitted after the run
  actually succeeds with the expected derived value. If the run fails or the
  derived total is wrong, we raise — we never record a placeholder number.
* **No LLM.** All benchmarks run under ``NullInference`` so they are reproducible
  on any machine and never depend on model availability.
* **Thresholds are explicit and asserted.** ``run_benchmarks`` returns the
  measured stats and, when ``fail_on_regression`` is set, asserts the per-case
  p95 wall time is under the declared cap. A slowed-down engine fails the suite,
  not silently "still green".
* **Derived answer is part of the contract.** Each case asserts its expected
  computed total, so a correctness regression is caught alongside a perf one.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .engine import WorkerEngine
from .inference import NullInference
from .models import RunStatus
from .store import WorkerStore
from .tools import build_registry


@dataclass
class CaseResult:
    name: str
    iterations: int
    times_ms: List[float]
    p50_ms: float
    p95_ms: float
    status: str  # RunStatus of the final iteration
    derived_total: Optional[str]  # the computed value the run reported (e.g. "188500.0")
    expected_total: Optional[str]


@dataclass
class BenchmarkReport:
    cases: List[CaseResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cases": [
                {
                    "name": c.name,
                    "iterations": c.iterations,
                    "p50_ms": round(c.p50_ms, 2),
                    "p95_ms": round(c.p95_ms, 2),
                    "status": c.status,
                    "derived_total": c.derived_total,
                    "expected_total": c.expected_total,
                }
                for c in self.cases
            ]
        }


def _percentile(values: List[float], q: float) -> float:
    """Linear-interpolation percentile; ``q`` in [0, 1]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _extract_derived_total(result) -> Optional[str]:  # pragma: no cover - unused
    return None


# Built-in benchmark cases. Each is a real worker request whose expected derived
# total is known. Thresholds (ms) are conservative caps on p95 wall time for the
# deterministic path; raise them only with an explicit, measured reason.
DEFAULT_CASES: List[Dict[str, Any]] = [
    {
        "name": "q2_revenue_total",
        "request": "What was total Q2 revenue?",
        "expected_total": "188500.0",
        "max_p95_ms": 4000.0,
    },
    {
        "name": "q2_by_channel",
        "request": "Q2 revenue by channel?",
        "expected_total": None,  # grouped result; correctness checked via SUCCESS only
        "max_p95_ms": 4000.0,
    },
]


def run_case(
    make_engine: Callable[[], WorkerEngine],
    request: str,
    iterations: int,
    expected_total: Optional[str] = None,
) -> CaseResult:
    """Run ``request`` ``iterations`` times and measure wall time.

    Fail-closed: the final iteration must be SUCCESS (or, when only a grouped
    answer is expected, SUCCESS), and when ``expected_total`` is set the run must
    have auto-derived that exact ``recompute_sum`` expected value. Otherwise we
    raise rather than emit a meaningless time.
    """
    times: List[float] = []
    last = None
    derived: Optional[str] = None
    for _ in range(iterations):
        engine = make_engine()
        t0 = time.perf_counter()
        last = engine.run(request)
        dt = (time.perf_counter() - t0) * 1000.0
        times.append(dt)
        # recover the derived total from verifications (real persisted records)
        vers = engine.store.find("verifications", run_id=last.run.id)
        for v in vers:
            if v.get("check") == "recompute_sum":
                derived = str(v.get("expected"))
                break
    assert last is not None
    # fail-closed: evidence of a real success is required
    if last.status != RunStatus.SUCCESS:
        raise AssertionError(
            f"benchmark run did not succeed: status={last.status} summary={last.summary!r}"
        )
    if expected_total is not None and derived != expected_total:
        raise AssertionError(
            f"benchmark derived total mismatch: got {derived!r}, expected {expected_total!r}"
        )
    return CaseResult(
        name=request,
        iterations=iterations,
        times_ms=times,
        p50_ms=_percentile(times, 0.50),
        p95_ms=_percentile(times, 0.95),
        status=last.status.value,
        derived_total=derived,
        expected_total=expected_total,
    )


def run_benchmarks(
    make_engine: Callable[[], WorkerEngine],
    cases: Optional[List[Dict[str, Any]]] = None,
    iterations: int = 3,
    fail_on_regression: bool = True,
) -> BenchmarkReport:
    """Run every case and return a :class:`BenchmarkReport`.

    When ``fail_on_regression`` is true, asserts each case's p95 is under its
    ``max_p95_ms`` cap. A slowdown fails loudly instead of going unnoticed.
    """
    cases = cases or DEFAULT_CASES
    report = BenchmarkReport()
    for c in cases:
        res = run_case(
            make_engine,
            c["request"],
            iterations=iterations,
            expected_total=c.get("expected_total"),
        )
        res.name = c["name"]
        report.cases.append(res)
        if fail_on_regression:
            cap = float(c.get("max_p95_ms", 1e9))
            if res.p95_ms > cap:
                raise AssertionError(
                    f"regression: {c['name']} p95={res.p95_ms:.1f}ms exceeds cap {cap:.1f}ms"
                )
    return report
