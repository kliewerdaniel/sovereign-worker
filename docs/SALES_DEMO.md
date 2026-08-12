# Sales Worker — End-to-End Demo

A walkthrough of the Sales Worker running the real `daily-run` loop, captured from
a clean environment. This is the artifact that makes "governed, attributable,
reproducible, independently verifiable" concrete instead of asserted. Run IDs and
evidence refs below are from an actual invocation on **2026-08-12** (no model — the
deterministic `NullInference` fallback, so the loop proves out without a live LLM).

> The Sales Worker is **one instance** of the general Sovereign Worker runtime.
> Nothing in this demo is sales-specific engine code — it's a worker identity
> (`sales_researcher` + `sales_outreach`), 16 opt-in tools, and two procedures, all
> driven by the same `WorkerEngine` that runs any other worker.

## What this proves

- Two worker instances run through one engine, with **separation of duties**
  (researcher discovers/researches/qualifies; outreach drafts/schedules — neither
  sends without the operator).
- Every figure in the report is **re-derived from the ledger** and every claim
  carries a `source_ref`.
- **External egress is held for approval**: 3 drafts are produced but `0` are sent.
  The run downgrades to a *Failed sales day* badge rather than pretending the
  targets were met.
- The whole run is **reconstructable without a model** via `sworker inspect`
  / `audit` / `replay`.

## Prerequisites

```bash
# Python 3.14.6 (Homebrew). Core has zero third-party deps.
/opt/homebrew/bin/python3.14 --version

# Optional read-only source of truth (DailySalesOS markdown). Without it the sales
# tools still run on the ledger; only ICP compile + daily-target parse degrade.
export DAILYSALESOS_ROOT=~/Documents/Projects/salesworkflow
```

## 1. Scaffold + seed a deterministic demo company

```bash
export SWORKER_HOME=/tmp/salesship_demo
export DAILYSALESOS_LEDGER=$SWORKER_HOME/company/Experiment_Ledger/experiments.db

python -m sworker sales init --force
# → sales workers written: DAILY_RESEARCH.yaml, DAILY_SALES_RUN.yaml,
#   sales_analyst.yaml, sales_outreach.yaml, sales_researcher.yaml
# → ICP compiled from DailySalesOS: 6 industries

python -m sworker sales seed
# → seeded: /tmp/salesship_demo/company/candidates.csv (3 candidates)
# → seeded: /tmp/salesship_demo/company/acme_robotics.md
```

`seed` is pure and idempotent: running it twice yields byte-identical files, so the
demo needs no private CRM or live API.

## 2. Run the daily loop

```bash
python -m sworker sales daily-run --source candidates.csv --limit 3
```

Output (verbatim, trimmed):

```
✓ Discover up to 3 candidate companies from candidates.csv      (sales_discover)
✓ Research every discovered lead from permitted company sources (sales_research)
✓ Qualify every researched lead deterministically from evidence  (sales_qualify)
✓ Produce the daily sales report against documented targets      (sales_metrics)
✓ Draft a personalised outreach message for each qualified lead  (sales_draft_outreach)
✓ Schedule the documented next action for each lead             (sales_schedule_followup)
✓ Move each reached lead to the contacted stage                  (sales_move_stage)
✓ Produce the daily sales report                                 (sales_metrics)

================================================================
DAILY SALES REPORT
================================================================
date:          2026-08-12
failed_sales_day: True
  MISS prospects_researched   3/20
  MISS outreach_sent          0/15
  MISS followups_sent         0/10
  MISS discoveries_completed  0/1
  MISS discoveries_scheduled  0/2
  - 3 outreach draft(s) awaiting approval
pending_approvals: 3

PER-RUN SUMMARY
  sales_researcher   SUCCESS   ok=yes
    inspect: sworker inspect run_3657cc8af687
  sales_outreach     SUCCESS   ok=yes
    inspect: sworker inspect run_6b6f034a096b
```

## 3. Inspect what actually happened (reconstructable without a model)

```bash
sworker inspect run_6b6f034a096b
```

```
RUN run_6b6f034a096b
  worker: sales_outreach
  status: SUCCESS
  01 STEP [DONE] Draft a personalised outreach message for each qualified lead
  02 STEP [DONE] Schedule the documented next action for each contacted lead
  03 STEP [DONE] Move each reached lead to the contacted stage
  04 STEP [DONE] Produce the daily sales report against the documented targets
  05 ACTION sales_draft_outreach [reversible] EXECUTED
  06 OBSERVATION ok drafted 3/3 qualified lead(s)
  07 EVIDENCE (observed) sales_draft_outreach: drafted 3/3 qualified lead(s) src=sales_draft_outreach
  08 ACTION sales_schedule_followup [reversible] EXECUTED
  09 OBSERVATION ok scheduled 0/3 qualified lead(s)
  10 EVIDENCE (observed) sales_schedule_followup: scheduled 0/3        src=sales_schedule_followup
  11 ACTION sales_move_stage [reversible] EXECUTED
  12 OBSERVATION ok moved 3/3 qualified lead(s) -> contacted
  13 EVIDENCE (observed) sales_move_stage: moved 3/3 -> contacted       src=sales_move_stage
  14 ACTION sales_metrics [read] EXECUTED
  15 OBSERVATION ok # Daily Sales Report — 2026-08-12
  16 EVIDENCE (observed) sales_metrics: # Daily Sales Report — 2026-08-12  src=sales_metrics
  17 ARTIFACT md /private/tmp/salesship_demo/daily_report.md
```

The researcher pass (`run_3657cc8af687`) shows the same shape at the discover/qualify
stage, and every evidence row carries a `source_ref` — e.g. the discover evidence
points at `candidates.csv#sha256:bf61c786c06a…`.

## 4. The approval gate

The outreach worker drafted 3 messages but **never sent one**. Sending is
`sales_record_sent` / `sales_bulk_send`, both risk `EXTERNAL` and `financial`, and
the researcher's `tools:` allowlist excludes them entirely. To complete egress the
operator approves each draft and resumes:

```bash
sworker approve <draft_approval_id>
sworker resume run_6b6f034a096b
```

Without that human step, the loop reports `pending_approvals: 3` and a `Failed sales
day` badge. That is **intended fail-closed behaviour**, not a defect.

## 5. Replay / audit

```bash
sworker audit run_3657cc8af687   # append-only, hash-chained event log
sworker replay run_3657cc8af687  # reconstruct the run from persisted records (no model)
```

The audit tail shows the hash chain intact: `evidence.recorded → artifact.created →
step.done → run.transition → run.status → run.finished`.

## Limitations (what still needs a human or a model)

1. **A model improves, not enables.** The deterministic path runs with no LLM
   (`NullInference`). What the model *adds* is tone-rewriting outreach bodies — and
   even then `outreach.validate_rewrite` rejects any rewrite that introduces a number
   not already in the deterministic draft. Without a model, drafts are plain but
   fact-complete.
2. **Research needs a per-lead source file.** `sales_research` reads permitted docs
   under `company/`. In this demo only `acme_robotics.md` exists (not named per
   candidate), so the researcher run reports `researched 0 lead(s): 0 evidence`. With
   real per-company source files, evidence + pain points attach. The qualification
   still runs deterministically (it scores from whatever evidence exists; a lead with
   zero evidence is refused, never scored from nothing).
3. **AST classification is not a sandbox.** `python.run`/`shell.exec` risk is derived
   by static `ast` walking + allowlisting; unrecognised imports/commands escalate to
   the highest tier. This *reduces* risk; it is not a container. Don't point a worker
   at an untrusted machine and call it isolated.
4. **Qualification judgment is not guaranteed true.** A `qualification.score` is
   computed deterministically from stored evidence and re-derived by the
   `sales_score_recomputes` check — but the *truth* of a pain-point or a fit judgment
   is only as good as the source it cites. The guarantee is on the **evidence trail**,
   not on the semantic claim.
5. **Targets come from one doc.** The daily minimums are parsed from
   `Metrics_Single_Source_of_Truth.md`. If that wording drifts, the
   `parse_daily_targets` regression test (`tests/test_sales_targets_real_doc.py`)
   fails CI rather than silently degrading the badge.
6. **Still manual today.** Approval/deny and `resume` are operator actions. The
   follow-up *scheduling* is automatic per stage rule, but the actual human follow-up
   and any live send remain human-in-the-loop by design.
