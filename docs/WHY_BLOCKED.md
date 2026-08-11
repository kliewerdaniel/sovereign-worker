# "Why Blocked?" Explainer (§65)

When a run ends `BLOCKED`, the *reason* is scattered across four stores:

* `run.error` — `incident_active`, `resource_exhausted`, unverifiable verification
* `degradations` table — safe-mode blocks, permission-denial degradations, model
  fallback, knowledge-uncompiled, secrets-unavailable, sandbox-host
* per-step `note` — safe-mode block, permission deny, approval rejection
* `incident` ledger — an open incident freezes the whole platform

Expecting an operator to cross-reference all four by hand is a fail-open trap:
any gap looks like "nothing is wrong." §65 closes that gap with a single
aggregator that answers "why was this blocked?" from the real data, in one place.

## `BlockExplainer` (`sworker/block_explainer.py`)

`explain_run(run_id)` reads every block signal above and returns:

* `run_id`, `status`, `was_blocked` (`True`/`False`/`None` — `None` means the
  record is missing or the status is unrecognized, never silently `False`)
* `reasons` — a list of `BlockReason` dicts: `source`, `kind`, `reason`,
  `severity`, `mitigation`, `detail`
* `summary` — a one-line verdict

`explain_workspace()` answers the same question at platform scope (incident
freeze + workspace-wide degradations).

## Fail-closed by construction

* **It only ever reports what the stores contain — it invents nothing.** There is
  no hardcoded "you were blocked because…" text; every reason traces to a real
  degradation row, a real step `note`, `run.error`, or the incident ledger.
* **Unknown inputs are `None`, not `False`.** A missing run record or an
  unrecognized status string yields `was_blocked = None` ("don't know") — the
  explainer refuses to claim "not blocked" when it cannot know.
* **A BLOCKED run with no logged reason is itself a finding.** If the run is
  `BLOCKED` but no reason is discoverable, the explainer emits a single `unknown`
  reason at `critical` severity and points at the audit log. Silence is surfaced,
  not swallowed.
* **Severity is preserved.** Degradation severities pass through; a step blocked
  by safe mode or a permission deny is `critical`; an approval rejection is
  `warning`. The summary counts critical vs. other so a "BLOCKED: 1 critical"
  cannot hide behind a pile of warnings.

## Surfaces

* CLI: `sworker why <run_id> [--json]` (per-run) and `sworker why --workspace
  [--json]` (platform). `cmd_why` prints the verdict + a per-reason table with
  the fix (`mitigation`) for each.
* Web (any authenticated session): the run-detail page shows a **"Why is this
  blocked? →"** link when the run is `BLOCKED`; `GET /why?run_id=` renders the
  HTML table; `GET /api/v1/why?run_id=` (and `GET /api/v1/why` for the workspace)
  returns the JSON payload — the same structure the CLI prints, so the three
  surfaces cannot drift.

## Proving it (anti-rot)

`tests/test_block_explainer.py` exercises the contract: missing run → `was_blocked
None` + `unknown`; incident freeze surfaced (critical); degradation table
surfaced with its mitigation; `run.error` tokens (`incident_active`,
`resource_exhausted`) mapped; per-step BLOCKED note surfaced + kinded
(`permission_denied`); a BLOCKED run with no reason still reports `unknown`
critical; a clean run reports `was_blocked False`; workspace explain aggregates
incident; and unknown inputs stay `None` (never `False`).
