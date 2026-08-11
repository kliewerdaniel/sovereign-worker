# Regression & Performance Benchmarks (§58/§59)

§68 proved the platform *works* end to end. §58/§59 make sure it keeps working —
and keeps working **fast** — by measuring the real deterministic engine path on
every run of the suite.

## `sworker/benchmark.py`

* `run_case(make_engine, request, iterations, expected_total)` — runs a real
  request `iterations` times under `NullInference`, recording wall time
  (milliseconds) for each. Returns `CaseResult` with `p50_ms` / `p95_ms`, the
  final `status`, and the `derived_total` recovered from the run's persisted
  `recompute_sum` verifications.
* `run_benchmarks(make_engine, cases, iterations, fail_on_regression)` — runs every
  case and returns a `BenchmarkReport`. With `fail_on_regression=True` it asserts
  each case's p95 is under its declared `max_p95_ms` cap.
* `_percentile` — linear-interpolation percentile (p50 / p95).

## Fail-closed, never fabricated

* **No measurement without a real success.** If the run is not `SUCCESS`, or the
  expected derived total doesn't match the persisted `recompute_sum` value, the
  case raises — it never emits a placeholder number. A correctness regression and
  a perf regression are both caught (not silently "green").
* **No LLM.** Everything runs under `NullInference`, so benchmarks are
  reproducible on any machine and independent of model availability.
* **Determinism is asserted.** The same request must derive the identical total
  on every iteration; nondeterminism in the planner/tool path trips the test.
* **Thresholds are explicit.** `DEFAULT_CASES` carries the known derived totals
  (e.g. `188500.0` for total Q2 revenue) and conservative p95 caps; raise a cap
  only with a measured, documented reason.

## Surfaces

* CLI: `sworker benchmark [--worker NAME] [--iterations N] [--no-fail] [--json]`.
  `--no-fail` reports without asserting thresholds (useful for a standing
  dashboard). Live sample: `q2_revenue_total p95≈38ms, derived=188500.0`.
* `tests/test_benchmark.py` (7 tests): percentile math; real timing + derived
  total; fail-closed on derived mismatch; determinism across iterations; all cases
  reported under threshold; regression flag tripped by a too-tight cap; `--no-fail`
  reporting mode.
