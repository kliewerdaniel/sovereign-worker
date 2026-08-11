# Graceful Degradation (§61)

The platform is built to **keep working** when a non-essential capability is
unavailable. A missing local model, an absent Atlas checkout, or an uninstalled
optional dependency should degrade the run — not crash it. But a degradation is
only acceptable if it is **visible, recorded, and never silently presented as a
clean success**. That is what §61 enforces.

## The principle

> A worker that quietly lost a safety-relevant capability is worse than one that
> stopped. Silence about a downgrade is itself a failure.

Degradations therefore follow three rules:

1. **Recorded** — every degradation is written to the `degradations` store
   table (`sworker/store.py`) *and* mirrored into the append-only audit log
   (`event == "degradation.recorded"`), so it is tamper-evident and queryable
   per run.
2. **Surfaced** — the `Run` record carries a `degradations` string list
   (`sworker/models.py`), printed by the CLI `run` command and returned in the
   web API. An operator reading a result can always tell "full capability" from
   "degraded".
3. **Fail-closed on critical** — a `critical` degradation (a safety/correctness
   capability that could not run) forces the run off full `SUCCESS` at
   finalize. A `warn` degradation is surfaced but does not downgrade the verdict.

## Where degradations originate

The ledger lives in `sworker/degradation.py`:

* `DegradationLedger` — `record(category, reason, *, severity, mitigation,
  run_id)`, `entries(run_id=)`, `any_critical(run_id=)`, `summary(run_id=)`.
* `DegradationRecord` — the persisted shape (`id`, `category`, `reason`,
  `severity`, `mitigation`, `run_id`, `created`).
* Severities: `WARN` and `CRITICAL` (anything else is treated as `CRITICAL`).
* Known categories: `MODEL_FALLBACK`, `KNOWLEDGE_UNCOMPILED`,
  `SECRETS_UNAVAILABLE`, `SANDBOX_HOST`.

Two are wired into the engine today (`sworker/engine.py`):

* **`model_fallback`** (warn) — `WorkerEngine.run()` records this when no
  language model is reachable (`self.llm.available()` is false). The run uses
  the deterministic fallback planner instead, and the operator is told.
* **`knowledge_uncompiled`** (warn) — when a run's tool registry includes
  knowledge tools but Hermes Atlas is unavailable (`knowledge.atlas_status()`),
  the knowledge tools still serve results, but on the degraded plaintext
  backend. The degradation is recorded rather than hidden.

The engine's `WorkerEngine._finalize` applies the fail-closed downgrade: if
`deg.any_critical()` and the computed status would be `SUCCESS`, it becomes
`PARTIAL_SUCCESS` with `run.error` set. The same method copies
`deg.summary()` onto `run.degradations` so the result is self-describing.

## Examples of intentional degradation (not bugs)

These already existed and are now *visible* rather than invisible:

* No local LLM → deterministic fallback planner (`sworker/engine.py`
  `_fallback_plan`). Recorded as `model_fallback`.
* Hermes Atlas absent → plaintext grep over company markdown
  (`sworker/knowledge.py`). Recorded as `knowledge_uncompiled` when a run uses
  knowledge tools.
* `cryptography` not installed → the secrets subsystem reports "unavailable"
  instead of encrypting (`sworker/secrets.py`). Category `SECRETS_UNAVAILABLE`
  is reserved for this.
* Docker sandbox requested but the CLI is absent → the sandbox fails **closed**
  (refuses to downgrade to host execution) rather than silently weakening
  isolation (`sworker/tools/sandbox.py`). That is a hard refusal, not a
  recorded `warn`.

## Proving it (anti-rot)

`tests/test_degradation.py` exercises the contract so this document cannot rot
into fiction. It asserts:

* a `record()` persists to the store **and** writes a `degradation.recorded`
  audit line;
* an unknown severity is treated as `CRITICAL` (fail-closed, never quietly
  downgraded);
* a `critical` degradation forces `WorkerEngine._finalize` to return
  `PARTIAL_SUCCESS` and populates `run.degradations`;
* a `warn` degradation is surfaced on the run but does **not** force a
  downgrade;
* `MODEL_FALLBACK` is recorded when a run has no reachable model;
* the human-readable `summary()` line format;
* entries round-trip through the store and `any_critical()` reflects them.

## What §61 does NOT do

It does not auto-heal, auto-restart, or auto-upgrade dependencies. Degradation
is reported; restoration is an operator action (the `mitigation` string on each
record says what to do). That keeps the platform local-first and free of silent
side effects.
