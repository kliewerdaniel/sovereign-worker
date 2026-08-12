# Architecture Assessment — Generalize the Runtime Substrate, Ship Sales as the Reference Worker

> Written **before** structural changes (Step 2 of the mission). Every claim below
> is grounded in the actual repository at the point of assessment, not in the mission
> prompt's assumptions. Where the prompt assumed a thing the code contradicts, the
> code wins.

## 0. TL;DR

The runtime/worker split the mission asks for **already exists** as a load-bearing
architectural fact in this repository. The engine is worker-agnostic and driven
entirely by a `WorkerConfig`. The Sales Worker is **already merged and verified** from
the prior arc (16 tools, 7 CLI subcommands, worker YAMLs, `DAILY_RESEARCH` /
`DAILY_SALES_RUN` procedures, 23 sales tests, a green daily loop). This mission is
therefore **not** a rewrite and **not** an implementation of the Sales Worker from
scratch — it is:

1. *Formalize* the runtime/worker boundary that already exists (name it, document the
   invariant, test it).
2. *Prove* it with a second worker domain (a contract test that runs a non-sales
   worker through the identical lifecycle with **zero** `if worker.name ==`
   branching in the engine).
3. *Add* the genuinely-missing product surfaces: `inspect <run_id>`, `sales daily-run`,
   a failure-injection test matrix, and the rewritten product docs (guarantee mapping,
   BUILDING_A_WORKER, DEMO, README/ARCHITECTURE/SECURITY).

Minimal changes. No engine rewrite. No second agent framework.

## 1. What already constitutes the Runtime

| Substrate concern | Where it lives | Evidence |
|---|---|---|
| Planning / execution loop | `engine.py::WorkerEngine.run` | 1255-line lifecycle: task→plan→step→action→observation→evidence→verification→artifact→approval→audit. Driven by `self.worker` + `self.registry.subset(worker.tools)`. |
| Permissions | `permissions.py` (`PermissionEngine`, `DecompositionGuard`) | 5-tier, AST-based, fail-closed. Classification from the *tool's declared risk*, never the model. DecompositionGuard blocks risk-laundering. |
| Tool dispatch | `tools/base.py` (`Tool`, `ToolRegistry`, `ToolContext`) | `registry.subset(worker.tools)` — a worker sees exactly the tools it named. `ToolContext` is built from `WorkerConfig`; a tool cannot widen its own boundary (`ctx.resolve` realpath-checks both sides). |
| Observations | `models.py::Observation` + `engine` | Every tool result becomes an `Observation`; never summarized in place. |
| Evidence | `evidence.py::EvidenceLedger` | `from_observation` mints evidence **only** from a real `Observation` or a real compiled-knowledge record. No model-prose path exists. `obs.ok is False → []`. |
| Verification | `verify.py` (`check`, `@check`) | Deterministic checks that *re-derive a number from source data*; no model in the loop. Mismatch → run degrades. |
| Artifacts | `models.py::Artifact` (sha256, claim_ids) | Persistence under `worker.artifacts_dir()`. |
| Approval | `approvals.py` + `models.py::Approval` | Quorum + min_role escalation (`§45`); fail-closed. |
| Persistence | `store.py::WorkerStore` | SQLite index + **append-only hash-chained** `audit.jsonl` (`verify_audit_chain`). |
| Replay | `explain.py::replay` | `mode="explain"` = read ledger (no model, deterministic); `mode="rerun"` = execute again. Explicitly distinct. |
| Audit | `store.iter_audit` + `cli audit` | Append-only event log reconstructable byte-for-byte. |
| State machine | `statemachine.py` | Explicit enforced transitions; illegal move → `IllegalTransition`. No silent status assignment. |
| Lifecycle / HITL | `lifecycle.py`, `degradation.py`, `incident.py`, `safemode.py` | Worker enable/disable/clone/archive; capability-loss ledger; incident freeze; safe-mode. |

**Conclusion:** the substrate is real and cohesive. The engine already satisfies the
Runtime Contract the mission describes (load → resolve policy → init state → plan →
dispatch → observe → evidence → verify → artifact → approve → finalize → audit →
replay). What is missing is the *name* and the *test*, not the behavior.

## 2. What is genuinely worker-specific

A worker is exactly a `WorkerConfig` (`config.py`) — a YAML file, by design (the docstring
states: "the thing that decides what an autonomous agent is allowed to do should be a
diffable, reviewable artifact in version control, not rows a model can edit").

`WorkerConfig` fields that are *purely* worker identity/policy/scope (correctly belong
in the worker, not the runtime):

- `name`, `role`, `instructions`, `knowledge`, `tools`, `procedures`
- `policy` (5-tier dispositions), `approval_policy` (quorum/min_role)
- `connectors` (default-deny external access), `fs_roots`, `shell_allow`, `env_allow`
- `message_allow` / `message_rate_limit`, `browser_*`, `egress_allow`, `dlp_rules`
- `sandbox`, resource caps (`max_runtime`, `max_actions`, `max_tool_calls`, …)
- `disabled`, `triggers`

The Sales Worker is just three of these YAMLs (`sales_researcher`,
`sales_outreach`, `sales_analyst`) plus a domain module (`sworker/sales/`) that
registers 16 `Tool` subclasses via `build_registry()` opt-in. The engine has **no**
sales-specific code path — sales-specificity lives entirely in `sworker/sales/`.

## 3. Where WorkerConfig mixes runtime concerns with worker config

Minor coupling, not architectural (these are already enumerated as worker-local and are
fine to keep there because they *are* worker policy):

- **Resource caps** (`max_runtime`, `max_actions`, `max_tool_calls`, `max_artifact_bytes`,
  …) live on `WorkerConfig` but are consumed by the *runtime* execution loop. This is a
  deliberate, correct placement: a worker declares its own ceilings; the runtime enforces
  them. The boundary is clean — the engine reads them, it does not own them as defaults
  for "the platform". No change required.
- The `path` field is an internal bookkeeping field populated at load time; excluded from
  `to_dict()` round-trip. Fine.

There is **no** place where `WorkerConfig` reaches into runtime internals or where the
runtime branches on a worker name. Confirmed by grep: zero occurrences of
`if worker.name ==` / `worker.name == "sales"` / `name == "sales"` in `engine.py`,
`statemachine.py`, `permissions.py`, `evidence.py`, `verify.py`. The one prior
engine edit (closed-world planner, prior arc) drops *any* unavailable tool generically —
it is domain-agnostic, not sales-specific.

## 4. Components reusable without modification

Everything in §1. Concretely proven reusable by the existing sales integration:
- `WorkerEngine.run` (same entry point sales uses).
- `Tool` / `ToolRegistry.subset` (sales tools are ordinary `Tool` subclasses).
- `EvidenceLedger` (wrapped, not replaced, by `sworker/sales/evidence.py`).
- `verify.check` + `@check` (sales checks are ordinary `@check` hooks, registered on
  import).
- `procedures.py` (`learn_from_run`, publish/rollback) — sales procedures are ordinary
  procedure YAMLs.
- `scheduler.py`, `knowledge.py`/Atlas bridge, `web.py`, `cli.py`.

## 5. Components requiring refactoring

**None structurally.** The only additions needed (not refactors):

- A second worker *instance* + a contract test that asserts the engine treats the two
  domains identically (Phase 3). This is a *test*, not a refactor — it protects against
  future drift. If the test reveals an engine branch keyed on worker identity, that is
  the single thing to fix; the assessment predicts it will not, because none exists today.
- `inspect <run_id>` (Phase 6) — new CLI command + web route, reading existing
  `store.iter_audit` / `store.find`. No engine change.
- `sales daily-run` (Phase 8) — new CLI orchestration over existing `run` + `metrics`; no
  engine change.

## 6. Components that should explicitly remain unchanged

- `engine.py` execution loop and lifecycle (do not rewrite).
- `statemachine.py` transition table.
- `permissions.py` AST classifier + `DecompositionGuard`.
- `evidence.py` minting rule (evidence only from observations).
- `verify.py` pure-function checks.
- `models.py` record shapes (`Provenance`, `RunStatus`, `Evidence`, `Claim`, …).
- `store.py` schema/registry and hash chain.
- The five-tier permission model and default-deny connectors.

## 7. Can the Sales Worker be implemented entirely through existing extension points?

**Yes — and it already was.** `sworker/sales/` adds no new execution architecture. It is:
domain dataclasses (`models.py`), an additive ledger schema (`schema.py`), a single-writer
repository, deterministic pipeline/qualification logic, 16 `Tool` subclasses at declared
risk tiers, worker YAMLs, and `DAILY_*` procedure YAMLs. The engine, permissions,
evidence, verification, approval, audit, and replay are all pre-existing substrate.

The proof the mission asks for — *"if the next worker were Research Worker instead of
Sales Worker, would we need another agent system?"* — is answered **now**: no, because the
engine is already domain-independent and sales proves it. This mission hardens that proof
with a second worker domain in the test suite and with the product docs that make the
boundary legible to a newcomer.

## 8. Risk register for this mission

- **Over-engineering:** the biggest risk is inventing a `Runtime` abstraction class the
  engine does not need. Mitigation: formalize via documentation + a `Runtime` *protocol
  description* and a contract test; do **not** introduce a wrapper class that re-implements
  `WorkerEngine`. If a mechanical contract is wanted, it is a `@runtime_contract` test
  asserting the lifecycle holds for ≥2 worker configs — not a new type.
- **Doc drift:** rewritten docs must match the shipped commands exactly (verified by
  running the demo in §11/12).
- **Unintended sales privilege in core:** every new capability must live in `sworker/sales/`
  unless it is demonstrably useful to ≥2 workers (e.g. an `inspect` timeline is substrate,
  not sales).
