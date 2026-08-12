# Sales Integration — DailySalesOS × Sovereign Worker (§71)

DailySalesOS defines **what the sales organization knows, wants and does**.
Sovereign Worker provides the **autonomous execution engine** that safely performs
those activities. The repos stay separate; this document is the integration
boundary.

```
DailySalesOS → Sales Domain (sworker/sales) → Sovereign Worker engine
  → Workers + Tools → Evidence / Verification → Sales Pipeline (14 stages)
```

---

## 1. Architecture assessment (written before implementation)

Read against the actual source of both repos, not their READMEs.

### 1.1 What already exists in `sworker` — confirmed

| Claim in the brief | Verdict | Actual source |
|---|---|---|
| Zero third-party runtime deps; Python 3.14; 443 tests pass | **confirmed** | `pyproject.toml`, `env -u PYTHONPATH /opt/homebrew/bin/python3.14 -m pytest tests/ -q` → `443 passed` |
| Pipeline REQUEST→…→AUDIT | **confirmed** | `engine.py` module docstring + `statemachine.py` |
| Workers are YAML with `tools:` allowlist and 5-tier `policy:` | **confirmed** | `config.py:19` `DEFAULT_POLICY`, `config.py:load_worker` rejects unknown risk keys and unknown dispositions |
| Five tiers `read/reversible/external/financial/destructive`, dispositions `auto/approve/deny` | **confirmed** | `models.py:RiskLevel`, `policy.py:RISK_CATEGORIES`, `policy.py:DISPOSITIONS` |
| `permissions.py` is AST-based and fail-closed | **confirmed** | `permissions.py:1-90`, `_PY_SAFE_MODULES` allow-list with escalation for anything unrecognised |
| `evidence.py` mints evidence only from real observations, with `source_ref` | **confirmed** | `EvidenceLedger.from_observation` returns `[]` when `obs.ok` is false; there is no model-prose path |
| `verify.py` deterministic checks | **confirmed, list corrected** | actual registered checks are `recompute_sum`, `recompute_delta_pct`, `row_count`, `file_exists`, `artifact_contains_evidence`, `provenance_chain`, `totals_match_source`, `schema`, `set_equality`, `regex`, `doc_section`. The brief's `delta` / `artifact_contains_evidence` names were approximate — `delta` is really `recompute_delta_pct` |
| `procedures.py::learn_from_run` + publish/rollback | **confirmed** | `learn_from_run` refuses to write a procedure when no action actually EXECUTED |
| `scheduler.py` `parse_cron`/`next_fire` | **confirmed** | `scheduler.py:89,104` |
| `knowledge.py` + `tools/knowledge.py` Atlas bridge, degrades to labelled grep | **confirmed** | `tools/knowledge.py:58-74` emits `[degraded: raw document grep, knowledge not compiled]` |
| store = sqlite index + append-only `audit.jsonl` under `<home>/.state/` | **confirmed, stronger than stated** | `store.py` — the audit log is also a **hash chain** (`_hash_record`, `verify_audit_chain`), and every record carries `org`/`workspace` tenant columns |
| CLI is one binary with subcommands | **confirmed** | `cli.py:build_parser` |
| Web UI stdlib `http.server`, 127.0.0.1, token+CSRF, `/api/v1/*` self-describing | **confirmed** | `web.py`, `/api/v1/openapi.json` |
| `inference.py` has `NullInference` deterministic fallback | **confirmed** | `inference.py:203`; also *refuses* non-local endpoints unless `--allow-external` |

Corrections worth recording:

1. **`store.py` tables are a fixed registry.** `TABLES` is a module-level dict and
   `put`/`find` raise `ValueError` for an unknown table. Sales domain records
   therefore must **not** be jammed into the sworker store — which is exactly what
   the brief wanted anyway (they belong in the DailySalesOS ledger). The sworker
   store keeps run/evidence/approval state only.
2. **Tools take no constructor arguments in the registry.** `build_registry()`
   registers module-level `TOOLS` singletons and the engine calls
   `registry.subset(worker.tools)`. A sales tool therefore cannot be handed a
   repository at construction time; it must resolve the ledger **from
   `ToolContext`** (`ctx.workspace`, bounded by `ctx.resolve`). This is a real
   constraint the brief did not mention, and it is what keeps sales tools inside
   the worker's filesystem boundary.
3. **`ToolContext.resolve` is the boundary.** Any sales tool that opens the
   ledger must resolve its path through `ctx.resolve(...)`, so a worker whose
   `fs_roots` excludes the ledger physically cannot reach it.
4. **`verify.py` checks are pure functions of `(spec, workspace)`**; they receive
   no store. Sales verification checks must therefore re-derive from the ledger
   file on disk, given a workspace-relative path — same discipline as
   `recompute_sum` re-reading the CSV.

### 1.2 What already exists in DailySalesOS — confirmed

- Knowledge layer: 23 markdown docs (2,947 lines) including every file named in
  the brief. `Follow_Up_System.md` exists (the brief cited it) — with concrete
  day-offset sequences per stage, which is what the Follow-Up worker needs.
- Data layer: `Experiment_Ledger/experiments.db`, schema in
  `experiments.db.schema.sql`. Actual tables: `experiments`,
  `experiment_metrics`, `prospects`, `outreach_touches`, **and `deals`** (the
  brief missed `deals`), plus views `experiment_summary` and `daily_activity`.
- `cli_prototype.py` supports exactly one command, `brief`.
- `CRM_Pipeline.md` defines **14 numbered stages**, but stage 10 is
  `Won / Lost` — one heading, two terminal outcomes. Modelled as **15 enum
  members** (`WON` and `LOST` separately) mapped onto the 14 documented stages,
  because a pipeline cannot have a single stage meaning both success and failure.
  Documented in `pipeline.py`.
- Claim tiers in `Hypothesis_Log.md`: CLAIM → HYPOTHESIS → OBSERVED (n<50) →
  CLIENT VERIFIED → CASE STUDY. Confirmed verbatim.
- Targets in `Metrics_Single_Source_of_Truth.md`: 20 prospects, 15 outreach,
  10 follow-ups, 1 discovery completed, 2 discoveries scheduled per day.
  Confirmed verbatim.
- ICP #1: Real Estate Professionals, score 4.6/5. Core offer: $2,500 audit.
- `Discovery_Rubric.md` has an explicit opportunity-score formula:
  `(Pain × Frequency × Revenue Impact × Automation Potential) / Implementation Difficulty`,
  normalised 0-100. The qualification engine uses **this** formula rather than
  inventing one.

### 1.3 Minimum integration surface

Nothing in the target architecture needed a new permission model, a new evidence
model, a new store, or a new scheduler. What was genuinely missing:

1. A **sales ontology** and its persistence (extension of the ledger schema).
2. A **repository** that is the only writer of those tables.
3. **Deterministic pipeline + qualification logic** (rubric formula, stage
   transition legality, claim tiers).
4. **Tool subclasses** exposing the above to workers under declared risk tiers.
5. **Worker YAML identities** and a `DAILY_SALES_RUN` procedure.
6. **CLI/API/UI surfaces** on the existing binary/server.
7. **Verification checks** that re-derive sales numbers from the ledger.

Everything else is reuse.

---

## 2. Permission model mapping

The existing five tiers, unchanged. No new mechanism. All sales capabilities are
exposed as the 16 `Sales*` `Tool` subclasses in `sworker/sales/tools/base.py`,
registered via `build_registry()` (opt-in — total registered tools: 40). The
names below are the **actual** tool names on the wire (the original design used
generic `lead.get` / `outreach.prepare` placeholders; the implementation uses a
`Sales`-prefixed namespace to coexist with the core registry without collision).

| Sales action | Tool (actual name) | Tier |
|---|---|---|
| inspect pipeline stages / history | `sales_pipeline_list` | `read` |
| explain an evidence chain | `sales_evidence_explain` | `read` |
| read a lead + its evidence | `sales_lead_detail` | `read` |
| list stale leads (no recent activity) | `sales_stale_leads` | `read` |
| pipeline summary (counts by stage) | `sales_pipeline_summary` | `read` |
| list follow-ups due | `sales_followup_due` | `read` |
| daily report vs documented targets | `sales_metrics` | `read` |
| discover candidates from local file | `sales_discover` | `reversible` |
| research a lead from permitted local docs | `sales_research` | `reversible` |
| score a lead (deterministic rubric) | `sales_qualify` | `reversible` |
| move a lead between pipeline stages | `sales_move_stage` | `reversible` |
| draft outreach (holds for approval) | `sales_draft_outreach` | `reversible` |
| schedule a follow-up from documented sequence | `sales_schedule_followup` | `reversible` |
| approve a drafted message for sending | `sales_approve_draft` | `external` |
| record that a send happened | `sales_record_sent` | `external` |
| bulk-send approved drafts | `sales_bulk_send` | `financial` |

With the default policy (`external: approve`, `financial: approve`) a sales
worker discovers, researches, scores, drafts, and schedules fully automatically
and **cannot send anything without a human approval**. Making sending automatic
requires editing the worker YAML — a reviewable diff. Separation of duties is
enforced by the worker `tools:` allowlists: `sales_researcher` gets
discover/research/qualify/metrics but **not** any `sales_approve_draft` /
`*_sent` tool, and `sales_outreach` gets draft/approve/record but **not**
discover/research.

## 3. Evidence model

The existing `EvidenceLedger`, wrapped (not replaced) by
`sworker/sales/evidence.py`:

- `SalesEvidence.attach(...)` persists a sales-scoped evidence row in the ledger
  (`sales_evidence`) whose `source_ref` is a `path#sha256:...` or a run/observation
  ref, and mirrors it into the run's `EvidenceLedger` when a run context exists.
- Sales claim types: `pain_point`, `icp_fit`, `contact_info`, `size_signal`,
  `tech_signal`, `hiring_signal`, `urgency_signal`, `budget_signal`.
- Claim tiers map onto sworker `Provenance` + DailySalesOS tiers:

| DailySalesOS tier | sworker `Provenance` | Meaning |
|---|---|---|
| CLAIM | `HYPOTHESIZED` | asserted, no observation |
| HYPOTHESIS | `INFERRED` | derived from other evidence |
| OBSERVED | `OBSERVED` | a tool actually saw it (n<50) |
| CLIENT_VERIFIED | `VERIFIED` | re-checked against client data |
| CASE_STUDY | `VERIFIED` + corroboration ≥2 independent sources |

A qualification score is refused (`INSUFFICIENT_EVIDENCE`) when the lead has no
evidence rows: `sales_qualify` never invents signals.

## 4. Schema extensions

Added to `Experiment_Ledger/experiments.db` by
`sworker/sales/schema.py::ensure_schema` (idempotent, additive; never drops or
rewrites an existing table):

`companies`, `contacts`, `leads`, `activities`, `opportunities`,
`pipeline_history`, `qualifications`, `sales_evidence`, `outreach_drafts`,
`tasks`, `followups`, `icp`, `pain_points`, `proposals`, `outcomes`.

`prospects`, `experiments`, `experiment_metrics`, `outreach_touches` and `deals`
are untouched; `leads.prospect_id` references `prospects(id)` so the pre-existing
prospect corpus stays the origin of record.

Score history is append-only: a re-score inserts a new `qualifications` row with
a new `version`; nothing is overwritten.

## 5. Degradation contract

`sales_qualify` computes the rubric score **deterministically** with no
model. If a local model is reachable it adds a `summary` and proposed pain-point
text, both stored as `HYPOTHESIZED` claims requiring their own evidence. With
`NullInference` the score is identical and the artifact records
`model_fallback` via the existing degradation ledger. A missing model never
changes a number.

## 6. Limitations

- Lead discovery from the open web is **not** wired to a live source: discovery
  reads a local candidate file (CSV/JSON) inside the worker's `fs_roots`, or the
  existing `prospects` table. Live sourcing requires an `egress_allow` +
  connector decision the user must make explicitly.
- `sales_record_sent` records that a send happened; actual delivery is
  `message.send` (outbox backend by default). No SMTP backend is bundled.
- Atlas is optional, so pain-point retrieval degrades to labelled grep over the
  DailySalesOS markdown.
- Discovery-call and proposal *content* generation is out of scope; the schema
  and stages exist, the generators do not.

## 6b. Running it (verified end-to-end)

The integration is implemented, not just designed. The commands below are the
exact ones used to produce a green daily loop against the real DailySalesOS
markdown + ledger. Python is `/opt/homebrew/bin/python3.14` (3.14); `sworker`
is run from its project root (no install needed).

```bash
export SWORKER_HOME=/tmp/salestest
export DAILYSALESOS_LEDGER="$SWORKER_HOME/company/Experiment_Ledger/experiments.db"
export DAILYSALESOS_ROOT=~/Documents/Projects/salesworkflow

# 1. Project the sales ontology into the ledger + write worker YAMLs.
/opt/homebrew/bin/python3.14 -m sworker sales init --force

# 2. Run the daily research loop (researcher: discover→research→qualify→report).
/opt/homebrew/bin/python3.14 -m sworker run sales_researcher "execute DAILY_RESEARCH" \
    -p DAILY_RESEARCH -i source=candidates.csv -i limit=20

# 3. Inspect the append-only audit trail.
/opt/homebrew/bin/python3.14 -m sworker audit run_428181be36e4

# 4. Re-derive every number from the ledger and re-check invariants.
/opt/homebrew/bin/python3.14 -m sworker verify --run run_428181be36e4
```

Real `run` output (deterministic fallback planner, no local LLM reachable):

```
RUN #0  SUCCESS
SUCCESS; 4 action(s) executed; 0 failed; 4 evidence item(s); 1 artifact(s)
  artifact: /private/tmp/salestest/daily_report.md
  DEGRADATIONS (capability reduced; run kept working):
    ! model_fallback: no reachable language model; using deterministic fallback planner [warn]
  replay audit: python -m sworker audit run_428181be36e4
```

The `daily_report.md` the run produced contains the **Activity vs targets**
table compiled from `Metrics_Single_Source_of_Truth.md` (the single source of
truth — targets are never hard-coded in the sales layer):

| Target (Metrics_Single_Source_of_Truth.md) | Metric | Actual | Target | Met |
|---|---|---|---|---|
| prospects_researched | leads_researched | 2 | 20 | NO |
| outreach_sent | outreach_sent | 0 | 15 | NO |
| followups_sent | followups_sent | 0 | 10 | NO |
| discoveries_completed | discoveries_completed | 0 | 1 | NO |
| discoveries_scheduled | discoveries_scheduled | 0 | 2 | NO |

Because the loop ran headless with no reachable LLM and no human approval gate,
no outreach was sent — the report correctly flags **Failed sales day** and the
bottleneck list shows exactly which daily minimums were missed. Every count is
re-derivable with `sworker verify` (check `sales_metrics_match_ledger`).

Audit trail excerpt (append-only, hash-chained in the store):

```
1786497877.661  run.started            runs          run_428181be36e4
1786497877.664  degradation.recorded   degradations  deg_b4105a714711
1786497877.667  plan.created           plans         plan_cff0e408176c
1786497877.674  action.proposed        actions       act_dbb7c771292e
1786497877.676  step.running           steps         step_624f124dfa8c
1786497877.681  evidence.recorded      evidence      ev_ab181c1a6988
1786497877.682  step.done              steps         step_624f124dfa8c
1786497877.705  artifact.created       artifacts     art_bfb4da4fbc96
1786497877.710  run.finished           runs          run_428181be36e4
```

`replay` reconstructs the run from the same ledger (46 events, 4 actions):

```bash
/opt/homebrew/bin/python3.14 -m sworker replay run_428181be36e4 --mode explain
# → {"mode": "explain", "run_id": "run_428181be36e4", "event_count": 46,
#     "actions": [4 tool-action records]}
```

## 6c. Test suite

`env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/ -q -p no:cacheprovider`
→ **466 passed** (443 baseline + 23 sales: models, schema round-trip, repository,
pipeline legality, deterministic qualification, evidence, checks fail-closed,
tool registry, e2e engine run, CLI).

---

## 7. Implementation status

**Complete and verified.** All seven missing pieces from §1.3 are implemented in
`sworker/sales/`:

1. `models.py` + `schema.py` — sales ontology persisted as an **additive
   extension** of `Experiment_Ledger/experiments.db` (never drops/alters the
   pre-existing `prospects`/`experiments`/`deals`/… tables).
2. `repository.py` — the single writer; enum round-trip, FK nullification,
   read-only `raw()`.
3. `pipeline.py` + `qualification.py` + `evidence.py` + `checks.py` —
   deterministic stage transitions, the documented rubric formula, evidence only
   from real observations, and 5 `@check` hooks (`sales_score_recomputes`,
   `sales_evidence_has_source`, `sales_outreach_approved_first`,
   `sales_pipeline_legal`, `sales_metrics_match_ledger`).
4. `tools/base.py` — 16 `Tool` subclasses at declared risk tiers, opt-in to the
   registry (40 tools total).
5. `cli.py` (7 subcommands) + `web.py` (`/api/v1/sales`, `/sales` page) +
   worker YAMLs + `DAILY_RESEARCH`/`DAILY_SALES_RUN` procedures.
6. 23 new tests; suite green at 466.
7. This document, corrected to the **actual** tool names and a verified run.

The next frontier (DailySalesOS v0.4 Sales Intelligence) remains Atlas-backed
claim-level retrieval and feeding real `experiment_metrics` back into ICP
ranking — a data-flow enhancement, not an architecture change.

### 7.1 Known limitations (as-built, not gaps)

- **No live web sourcing.** `sales_discover` reads a local candidate file
  (CSV/JSON) inside the worker's `fs_roots` or the existing `prospects` table.
  Live sourcing needs an explicit `egress_allow` + connector decision.
- **No SMTP/outbox backend bundled.** `sales_record_sent` records that a send
  happened; actual delivery (`message.send`) is out of the sales layer.
- **Headless runs use `NullInference`.** The deterministic fallback planner does
  the discover→research→qualify→report sequence; scores are identical with or
  without a model. A reachable local LLM at `SWORKER_LLM_URL` would add
  hypothesized pain-point text (stored as `HYPOTHESIZED`, requiring its own
  evidence) without ever changing a numeric score.
- **Discovery-call and proposal *content* generation is out of scope**; the
  schema and pipeline stages exist, the generators do not.
