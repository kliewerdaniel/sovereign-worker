# `sworker/sales` — DailySalesOS × Sovereign Worker boundary layer

This package is **not** a re-implementation of Sovereign Worker. It is the thin
integration boundary that projects the DailySalesOS sales domain (markdown
knowledge + the `Experiment_Ledger` sqlite database) into Sovereign Worker's
existing execution engine — five-tier permissions, `EvidenceLedger`,
`verify.py` checks, `procedures.py`, `scheduler.py` and the sqlite + hash-chained
`audit.jsonl` store are all reused as-is.

The authoritative design document is `docs/SALES_INTEGRATION.md`. This README is
the map of the package itself.

## Layout

| Module | Responsibility |
|---|---|
| `models.py` | Sales ontology as dataclasses (`Lead`, `Company`, `Contact`, `Qualification`, `PipelineStage` (15 members), `ClaimTier`, …). |
| `schema.py` | `ensure_schema()` — idempotent, **additive** DDL. Never drops or alters the pre-existing `prospects`/`experiments`/`deals` tables. |
| `repository.py` | `SalesRepository` — the **single writer** to the sales tables. Enum round-trip, FK nullification, read-only `raw()`. |
| `pipeline.py` | 14 documented CRM stages + split `WON`/`LOST` → 15 enum members; transition legality via `can_move`. |
| `qualification.py` | Deterministic opportunity score from `Discovery_Rubric.md`'s formula. Refuses to score a lead with no evidence. |
| `evidence.py` | `SalesEvidence.attach` — evidence only from real observations; `source_ref` is `path#sha256:…` or a run/observation ref. |
| `knowledge.py` | The **compiler**: parses the DailySalesOS markdown (ICP, offer, targets, follow-up sequences) into the ontology, recording `source_doc` + line for every value. |
| `discovery.py` / `research.py` / `outreach.py` / `followup.py` / `metrics.py` | Domain functions used by the tools (local-only; never egress). |
| `checks.py` | 5 `@check` hooks: `sales_score_recomputes`, `sales_evidence_has_source`, `sales_outreach_approved_first`, `sales_pipeline_legal`, `sales_metrics_match_ledger`. |
| `tools/base.py` | 16 `Sales*` `Tool` subclasses at declared risk tiers, opt-in to the registry (`build_registry()` → 40 tools total). |
| `cli.py` | `sworker sales …` group: `init`, `icp`, `pipeline`, `lead`, `metrics`, `verify`, `templates`. |
| `web.py` | `/api/v1/sales` endpoints + the `/sales` page, on the existing stdlib server. |
| `templates/` | Worker YAMLs (`sales_researcher`, `sales_outreach`, `sales_analyst`) + `DAILY_RESEARCH.yaml` / `DAILY_SALES_RUN.yaml` procedures. |

## Tools (actual names)

All 16 sales capabilities are exposed with a `sales_` prefix so they coexist with
the core registry:

`read` tier — `sales_pipeline_list`, `sales_evidence_explain`, `sales_lead_detail`,
`sales_stale_leads`, `sales_pipeline_summary`, `sales_followup_due`, `sales_metrics`.
`reversible` — `sales_discover`, `sales_research`, `sales_qualify`, `sales_move_stage`,
`sales_draft_outreach`, `sales_schedule_followup`.
`external` — `sales_approve_draft`, `sales_record_sent`.
`financial` — `sales_bulk_send`.

With the default policy (`external: approve`, `financial: approve`) a worker
discovers, researches, scores, drafts and schedules automatically and **cannot
send without a human approval**. Separation of duties is enforced by worker
`tools:` allowlists — the researcher has no `sales_approve_draft` / `*_sent`
tools; outreach has no discover/research tools.

## Running

```bash
export SWORKER_HOME=/tmp/salestest
export DAILYSALESOS_LEDGER="$SWORKER_HOME/company/Experiment_Ledger/experiments.db"
export DAILYSALESOS_ROOT=~/Documents/Projects/salesworkflow

python3.14 -m sworker sales init --force
python3.14 -m sworker run sales_researcher "execute DAILY_RESEARCH" \
    -p DAILY_RESEARCH -i source=candidates.csv -i limit=20
python3.14 -m sworker audit run_428181be36e4
python3.14 -m sworker verify --run run_428181be36e4
```

Without a local LLM at `SWORKER_LLM_URL` the engine degrades to its deterministic
fallback planner (`model_fallback` warning) — the score and report numbers are
identical; only hypothesized prose is omitted.

## Tests

`env -u PYTHONPATH -u PYTHONHOME python3.14 -m pytest tests/ -q -p no:cacheprovider`
→ **466 passed** (443 baseline + 23 sales).

## Constraints (non-negotiable)

- **Zero third-party deps** in the core; this package is stdlib-only too.
- **Fail-closed.** Unknown tool, unknown risk key, or unparseable input → deny,
  never guess. Evidence requires a real `source_ref`; qualification refuses
  no-evidence leads.
- **Ledger is additive.** Pre-existing tables are never dropped or altered.
- **Closed-world planning.** The fallback planner converts unknown tools into
  reasoning-only steps.
- **No fabrication.** Nothing in the database is unattributable; every claim is
  traceable to a `source_doc` or observation.
