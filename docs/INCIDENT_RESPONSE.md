# Incident Response (§63)

Incident response is the operator's control surface for "the platform is in a bad
state and I need to freeze it, on the record." It is built directly on two
earlier fail-closed primitives: **safe mode** (§62, which stops tool actions) and
the **append-only, hash-chained audit log** (§13, which makes the incident
timeline tamper-evident).

## What an incident does

* **Freezes execution.** Declaring an incident engages safe mode `locked`
  (§62), so no new tool action can run while the incident is live. The worker
  may still plan and propose; it executes nothing.
* **Refuses new runs.** `WorkerEngine.run()` checks for an open incident *before*
  any planning or execution. If one is open, the run is reported `BLOCKED` with a
  `critical` `incident_active` degradation — fail-closed, never silently dropped.
* **Records every transition.** `open`, `close`, `lockdown`, and a rejected
  second `open` are all written to the audit log (`event` names
  `incident.opened`, `incident.closed`, `incident.lockdown`,
  `incident.rejected`), so the incident timeline can be replayed and verified.
* **Never auto-stands-down.** Closing an incident records the closure but does
  **not** clear safe mode. The operator must explicitly run
  `sworker safemode off`. A safety control is never quietly lowered by closing a
  ticket.

## Fail-closed invariants

* **One incident at a time.** Opening a second incident while one is already
  open is *rejected* (recorded, not silently superseded).
* **Corrupt state = open.** The open/closed flag is persisted in `meta_kv`
  (`scope == "incident"`), tenant-scoped. An unrecognised persisted value is read
  back as *open + locked* — a bad value can only ever increase restriction.
* **No guessing during an incident.** Blocking is default-deny; the only way to
  change the level is an explicit operator action.

## Controls

* CLI: `sworker incident [status|--json|--timeline]`, `sworker incident open
  "<summary>"`, `sworker incident lockdown "<summary>"`, `sworker incident close
  [--note ...]`.
* Web (admin only): `GET /api/v1/incident` (status), `POST /api/v1/incident`
  with `action` of `open` / `lockdown` / `close`.

## Implementation

`sworker/incident.py` — `IncidentLedger` over `meta_kv` + audit log: `active()`,
`status_dict()`, `list_incidents()` (replays `incident.*` audit events),
`open()` (engages `locked`, rejects a second open), `lockdown()` (idempotent
freeze), `close()` (records closure, leaves safe mode untouched). The engine
(`sworker/engine.py`) reads `IncidentLedger(store).active()` once per `run()` and
returns a `BLOCKED` `RunResult` with an `incident_active` `critical` degradation
when an incident is open. The state machine (`sworker/statemachine.py`) was
extended so `PLANNING -> BLOCKED` is a legal transition — an incident freeze
legitimately blocks a run before any execution.

## Proving it (anti-rot)

`tests/test_incident.py` exercises the contract: open engages `locked`; a second
open is rejected (and both events land in the timeline); `lockdown` is idempotent
and locks; `close` does not auto-clear safe mode; `close` is a no-op when inactive;
an engine run under an open incident is `BLOCKED` with `incident_active`;
without an incident the run is not incident-blocked; a corrupt persisted state
reads back as open (fail-closed); `status_dict` shape.
