# Security Events + Dashboard (§64)

The platform already writes every security-relevant transition to the
append-only, hash-chained audit log (§13): incidents (§63), safe-mode changes
(§62), permission denials, auth/session lifecycle, approval escalations, policy
and secret changes, graceful-degradation records (§61), and fail-closed run
transitions. That raw stream is complete but it is a firehose.

§64 curates it. A fixed **event catalog** maps audit `event` names to a severity
(`info` / `notice` / `warning` / `critical`) and a human label, producing a
focused, queryable **security event feed** — and surfaces it in the operator
dashboard plus a dedicated security page and API.

## Fail-closed by construction

* **The catalog is a lens, not a source.** The raw audit log remains the
  source of truth (tamper-evident). The feed only ever reports what the audit
  log *actually contains*. It never invents events.
* **Allow-list, not deny-list.** An event is surfaced only if it is in the
  catalog. Anything not catalogued stays in the raw log — adding a new audited
  action can never silently lose history, but it also won't appear until it is
  added to the catalog.
* **A degradation record carries its own severity.** When a
  `degradation.recorded` event is surfaced, the feed honours the record's own
  severity rather than a fixed one.
* **Run transitions are only "security events" when they land on a fail-closed
  state** (`BLOCKED` / `CANCELLED` / `DENIED`). Ordinary lifecycle transitions
  are ordinary noise and are omitted.
* **Chain integrity is visible, not hidden.** The feed and the dashboard both
  report the `verify_audit_chain()` verdict, so a tampered log is *shown*, never
  silently swallowed.

## Controls

* CLI: `sworker security [--json] [--kind <kind>] [--limit N]`.
* Web (any authenticated session): `GET /security` (HTML feed), `GET
  /api/v1/security` (JSON: `audit_chain_ok`, counts by kind, recent events).
  Linked from the dashboard nav.

## Implementation

`sworker/security_events.py` — `SecurityEvents(store)` over the audit log:
`recent(limit, kinds, since)`, `counts_by_kind()`, `chain_ok()`. The internal
catalog (`_EVENT_CATALOG` / `_LABELS`) is the allow-list; `_severity_for` applies
the degradation and run-transition special cases. The dashboard
(`sworker/web.py`) gained `render_security()` (HTML) wired to `GET /security`,
and `_security_payload()` wired to `GET /api/v1/security`; both reuse the same
catalog so the page, the API, and the CLI cannot drift apart.

## Proving it (anti-rot)

`tests/test_security_events.py` exercises the contract: incident open/close are
surfaced (critical / notice); safe-mode change is a warning; a degradation
record's own severity is honoured; a `run.transition` is only a security event
when it lands on a fail-closed state; the audit-chain verdict is reported; kind
filtering works; the feed never invents events (every surfaced event is in the
allow-list); both tier levels appear.
