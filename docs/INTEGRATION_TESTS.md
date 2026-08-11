# End-to-End Integration Tests (§68)

The hardening from §42–§66 proves each subsystem in isolation. §68 proves the
**whole platform stack works together** through the same public `WorkerEngine`
API a real deployment uses — with no mocks, no cloud, and no language model.

## `tests/test_e2e.py`

A temp workspace is seeded with a CSV + worker YAML; the engine runs in
**deterministic fallback** (`NullInference`) so the tests are reproducible on any
machine. Every assertion reads *persisted* state, never an in-memory object the
engine happened to return.

Coverage (real behavior, not stubs):

* **Full run + derived verification chain.** A "total Q2 revenue?" request must
  reach `SUCCESS`, state the derived figure (`188,500`), emit ≥1 artifact, and
  auto-mint `recompute_sum` verifications that re-sum the *same source rows* and
  `PASS` — the product's core promise that no number is stated without being
  independently re-derivable.
* **Audit chain intact.** After the run, `store.verify_audit_chain()["ok"]` is
  `True` with `checked > 0` — every mutating step is hash-chained and
  tamper-evident.
* **Incident freeze gates real runs (§63).** Opening a `IncidentLedger` incident
  then calling `engine.run()` must return `BLOCKED` with `run.error ==
  "incident_active"` — fail-closed, not silently dropped — and the block itself
  is still recorded (chain intact).
* **Safe-mode lockdown gates real runs (§62).** `SafeMode(store).lock()` then a
  run must **not** reach `SUCCESS`; it must be `BLOCKED`.
* **System-status aggregates a real incident (§66).** `SystemStatus(store)
  .compose()` over an open incident returns `verdict == CRITICAL`, with the
  `incident` control carrying `critical`.
* **Cancellation (§11).** A run sitting `AWAITING_APPROVAL` moves to `CANCELLED`
  on `engine.cancel()` and re-cancelling the already-terminal run is idempotent
  (same `run.id`, no error).
* **Run is reconstructable from the ledger (spec principle #4).** The persisted
  `runs`/`steps`/`evidence`/`verifications` records fully describe the run; the
  derived total remains re-derivable.

## Why this exists

Unit tests for each subsystem can pass while the *composition* breaks (a guard
fires in the wrong order, a freeze path returns the wrong status, the status
surface disagrees with the engine). §68 is the regression net that catches those
by driving the real engine end to end.
