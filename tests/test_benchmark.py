"""§58/§59 — benchmark harness tests.

The harness must (a) measure real wall time, (b) only emit a measurement after a
genuine SUCCESS with the expected derived total (fail-closed — never fabricate a
number when the run fails), (c) catch a correctness regression, and (d) flag a
perf regression against the declared p95 cap.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sworker.config import Workspace, get_worker
from sworker.store import WorkerStore
from sworker.engine import WorkerEngine
from sworker.inference import NullInference
from sworker.tools import build_registry
from sworker.benchmark import run_case, run_benchmarks, _percentile, BenchmarkReport


SALES_CSV = """region,quarter,revenue,orders
North,Q1,42000,1320
North,Q2,51000,1480
South,Q1,31000,980
South,Q2,35500,1100
Online,Q1,88000,4200
Online,Q2,102000,5100
"""

WORKER_YAML = """name: acme-analyst
role: Acme Coffee business analyst
instructions: |
  Compute figures from the CSVs with data.query; never state a number you did
  not derive.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: auto
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
"""


@pytest.fixture()
def ws(tmp_path):
    home = tmp_path / "acme"
    home.mkdir()
    (home / "company").mkdir()
    (home / "company" / "sales.csv").write_text(SALES_CSV)
    (home / "workers").mkdir()
    (home / "workers" / "acme-analyst.yaml").write_text(WORKER_YAML)
    os.environ["SWORKER_HOME"] = str(home)
    w = Workspace(str(home))
    w.ensure()
    return w


def make_engine(ws):
    worker = get_worker("acme-analyst", ws)
    store = WorkerStore(ws.state_dir)
    return WorkerEngine(worker, store, inference=NullInference(), registry=build_registry())


def test_percentile_linear_interp():
    v = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(v, 0.5) == 25.0
    assert _percentile(v, 0.0) == 10.0
    assert _percentile(v, 1.0) == 40.0


def test_case_measures_real_time_and_derived_total(ws):
    res = run_case(lambda: make_engine(ws), "What was total Q2 revenue?", iterations=2,
                   expected_total="188500.0")
    assert res.iterations == 2
    assert res.p50_ms > 0 and res.p95_ms >= res.p50_ms  # real measurement
    assert res.status == "SUCCESS"
    assert res.derived_total == "188500.0"


def test_case_fails_closed_when_derived_mismatch(ws):
    # wrong expected total must raise, never emit a fake measurement
    with pytest.raises(AssertionError):
        run_case(lambda: make_engine(ws), "What was total Q2 revenue?", iterations=1,
                 expected_total="999999.0")


def test_case_is_deterministic_across_iterations(ws):
    # a regression that introduces nondeterminism must be caught: the same
    # request must derive the identical total on every iteration
    res = run_case(lambda: make_engine(ws), "What was total Q2 revenue?", iterations=3,
                   expected_total="188500.0")
    # derived_total is fixed by expected_total assertion; verify it's stable+jittered
    assert res.derived_total == "188500.0"
    # times vary slightly but stays positive and bounded
    assert all(t > 0 for t in res.times_ms)


def test_run_benchmarks_reports_all_cases_and_thresholds(ws):
    report = run_benchmarks(lambda: make_engine(ws), iterations=2, fail_on_regression=True)
    assert isinstance(report, BenchmarkReport)
    names = {c.name for c in report.cases}
    assert {"q2_revenue_total", "q2_by_channel"} <= names
    for c in report.cases:
        assert c.status == "SUCCESS"


def test_regression_flag_tripped_by_slow_cap(ws):
    # a deliberately tiny cap must trip the regression assertion
    cases = [{
        "name": "q2_revenue_total",
        "request": "What was total Q2 revenue?",
        "expected_total": "188500.0",
        "max_p95_ms": 0.0001,
    }]
    with pytest.raises(AssertionError):
        run_benchmarks(lambda: make_engine(ws), cases=cases, iterations=2, fail_on_regression=True)


def test_no_fail_mode_reports_without_asserting(ws):
    cases = [{
        "name": "q2_revenue_total",
        "request": "What was total Q2 revenue?",
        "expected_total": "188500.0",
        "max_p95_ms": 0.0001,
    }]
    # with fail_on_regression=False it must still succeed and report
    report = run_benchmarks(lambda: make_engine(ws), cases=cases, iterations=2, fail_on_regression=False)
    assert report.cases[0].p95_ms > 0.0001
