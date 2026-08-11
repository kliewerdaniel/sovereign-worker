# Composable System Status (§66)

By §65 the platform had five independently-published hardening signals, each behind its own command, web page, and API route:

* `SafeMode.level()` — `off` / `readonly` / `locked`
* `IncidentLedger.status_dict()` — `active` / `closed` (platform freeze)
* `DegradationLedger.any_critical()` + `summary()` — graceful-degradation ledger
* `SecurityEvents` + `store.verify_audit_chain()` — §64 feed + chain integrity
* `BlockExplainer.explain_workspace()` — §65 aggregated block reasons

Answering "is this platform healthy right now?" meant opening five places. §66 adds a **thin, uniform composable surface** on top of those existing signals — it does not re-implement them and does not modify their internals, so their tests keep holding.

## `sworker/system_status.py`

* `ControlSnapshot` — one shape for every control: `name`, `severity`
  (`ok`/`warning`/`critical`/`unknown`), `status` (real one-liner), `source`
  (the `module.symbol` that produced it), `detail`.
* Five adapter functions (`snapshot_safemode`, `snapshot_incident`,
  `snapshot_degradation`, `snapshot_security`, `snapshot_blocked`) — each reads
  **only** its subsystem's existing public surface.
* `SystemStatus(store).compose()` runs every adapter and returns
  `{verdict, generated_at, controls[]}`. `verdict` is worst-severity-wins across
  the controls (`critical > unknown > warning > ok`).
* `ADAPTERS` is an ordered list of callables — adding a future hardening control
  is one appended adapter; nothing else changes.

## Fail-closed by construction

* **It only reports what the subsystems report — it invents nothing.** No
  hardcoded "you are healthy" text; the verdict is derived from the real control
  severities, and every snapshot cites its source symbol.
* **A control that raises is `unknown`, never `ok`.** One broken probe can never
  paint the platform green.
* **Verdict = worst severity, with `unknown` outranking `warning`.** A probe that
  couldn't answer must not be assumed benign.
* **No noise suppression.** Every control is listed — including `ok` ones — so a
  reader sees what was checked, not just what failed.

## Surfaces

* CLI: `sworker status [--json]` — prints the verdict + a per-control table.
* Web (any authenticated session): `GET /status` (HTML) and `GET /api/v1/status`
  (JSON). The `/status` page links to `/security` and `/why?workspace=1`.

## Proving it (anti-rot)

`tests/test_system_status.py` covers: all five controls registered; clean
workspace → `ok`; open incident → `critical`; safe-mode locked → `critical`;
critical degradation → `critical`; a broken probe → `unknown` (not `ok`);
severity ranking; and that every snapshot is a real `ControlSnapshot` citing its
source with non-empty status (never invented).
