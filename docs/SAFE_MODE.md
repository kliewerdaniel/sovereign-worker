# Safe Mode (§62)

Safe mode is a single operator switch that makes a worker **fail closed**
instead of continuing to act on the world. It exists for the moment an operator
suspects the platform is in a bad state — a model that may be misbehaving, an
environment they don't trust, or a security incident in progress — and wants the
worker to stop *doing* things while it keeps *observing*.

## Levels

* **`off`** — normal operation; safe mode imposes no restriction.
* **`readonly`** — blocks every action whose risk is higher than `READ`. Read-only
  retrieval (filesystem reads, local data queries, knowledge search) is still
  permitted, because it cannot change the world. Anything that writes, sends,
  spends, or destroys is blocked.
* **`locked`** — blocks **every** action that would invoke a tool. The worker may
  still plan and propose, but it executes nothing. This is the "freeze the
  platform" switch for an active incident.

## How it behaves (fail-closed, never silent)

Every block safe mode causes is:

* **recorded** as a `critical` degradation (`safe_mode_block`) in the audit log
  (`event == "degradation.recorded"`), so it is tamper-evident and queryable
  per-run via the `degradations` table;
* **surfaced** on the run result (`Run.degradations`), and because it is
  `critical`, the run is reported `BLOCKED` rather than a clean `SUCCESS`
  (the §61 fail-closed downgrade applies);
* **overriding** — safe mode is checked *before* the normal approve/deny
  handling, so an action cannot slip through an auto-approved path during an
  incident.

The toggle is persisted in `meta_kv` (`scope == "safemode"`) so it survives
restarts and is tenant-scoped per workspace store. A corrupted or unrecognised
persisted value is read back as `locked` — a bad value can only ever *increase*
restriction, never silently disable the guard. The only ways to change the level
are explicit operator actions; nothing in the runtime auto-downgrades it.

## Controls

* CLI: `sworker safemode [status|--json]`, `sworker safemode on` (→ readonly),
  `sworker safemode off`, `sworker safemode readonly`, `sworker safemode locked`.
* Web (admin only): `GET /api/v1/safemode` (status), `POST /api/v1/safemode`
  with `level` (`on`/`off`/`readonly`/`locked`).

## Implementation

`sworker/safemode.py` — `SafeMode` controller over `meta_kv`: `enabled()`,
`level()`, `set_level()`, `enable()`, `disable()`, `lock()`, `is_blocked(risk)`,
`reason(risk)`, `status_dict()`. `is_blocked` is fail-closed: an unknown risk
blocks, and `locked` blocks all tool actions including ones of undeterminable
risk. The engine (`sworker/engine.py`) reads `SafeMode(store)` once per `run()`
and applies the block at permission-eval time, recording a `critical`
`safe_mode_block` degradation on the first blocked action.

## Proving it (anti-rot)

`tests/test_safemode.py` exercises the contract:

* default level is `off`; enable/disable + explicit-level round-trips;
* unknown level rejected; a corrupted persisted level fails closed to `locked`;
* `readonly` blocks everything above `READ` (and unknown risk); `locked` blocks
  all tool risks including undeterminable (`None`) risk;
* `status_dict` shape;
* two engine-integration runs: under `readonly` and `locked` a real
  `WorkerEngine` run that would write an artifact is reported `BLOCKED` with a
  `safe_mode_block` degradation; with safe mode `off` no such degradation is
  injected.
