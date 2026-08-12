# §71b — Building Another Worker on the Sovereign Worker Runtime

This document is the success test of the runtime/worker boundary: **if the next
worker were a Research Worker instead of the Sales Worker, would we need to build
another agent system?** The correct answer is *no* — you define another worker
*instance* on the same runtime. You do **not** fork the engine, the planner, the
permission model, the audit log, the verification layer, or the replay/inspect
tools. You only declare a new `WorkerConfig` and — if the domain needs new
capabilities — add new `Tool` subclasses that opt into the existing registry.

The Sales Worker (`sworker/sales/`) is the first serious reference implementation
of this boundary. It added 16 sales tools, 3 worker YAMLs, 2 procedures, and a
boundary package — and **not one line of `engine.py` changed for the domain**.
The same path is open to you for any domain.

---

## 0. What a "worker" actually is

A worker is a `WorkerConfig` (see `sworker/config.py`): a pure data object,
never edited by the model at runtime. Its fields:

| field | meaning |
|-------|---------|
| `name` | identity; used only for display + audit, **never** for branching logic |
| `role` / `instructions` | the system prompt given to the planner |
| `tools` | the **allow-list** of tool names this worker may call |
| `procedures` | named, versioned procedure YAMLs it may run |
| `policy` | per-tier authority: `read` / `reversible` / `external` / `financial` / `destructive` → `auto` / `approve` / `deny` |
| `knowledge` | RAG scopes the planner may search |
| `fs_roots`, `shell_allow`, `env_allow`, `egress_allow`, `browser_*`, `message_*` | capability + boundary config |
| resource limits | `max_steps`, `max_actions`, `max_runtime`, … (all fail-closed) |

That is the *entire* surface. Everything below shows how to define a new one.

---

## 1. The minimal path: a new worker from existing tools

If your domain can be served by tools the runtime already has (`fs.*`, `data.*`,
`knowledge.*`, `http.*`, `python.run`, `shell.exec`, `message.*`, `browser.*`),
you need **only a YAML file**. No Python.

```yaml
# .sworker/workers/research_assistant.yaml
name: research_assistant
role: Local-first research analyst. Reads permitted sources, queries them, and writes a cited report.
instructions: |
  Operate inside the worker filesystem boundary. Read permitted files with fs.read,
  compute numbers with data.query, and write the report with fs.write.
  Every claim must cite a source_ref you actually read.
tools:
  - fs.read
  - fs.list
  - fs.write
  - data.query
  - knowledge.search
policy:
  read: auto
  reversible: auto
  external: approve      # egress must be human-approved
  financial: approve
  destructive: deny      # never let this worker delete anything
fs_roots:
  - data
max_steps: 12
max_actions: 24
```

Run it:

```bash
python -m sworker run research_assistant "Summarise data/notes.md into report.md"
```

That's a worker. It inherits the full runtime for free: append-only audit,
plan/permission enforcement, evidence provenance, verification, `inspect`,
`replay`, `why`, `audit`. **Nothing else to build.**

---

## 2. The boundary package pattern (for a non-trivial domain)

If your domain needs state the core doesn't model (like sales needs a CRM /
pipeline / qualification ledger), follow the Sales Worker's exact pattern:

1. **Add a boundary package**, e.g. `sworker/<domain>/`, that depends *only* on
   the runtime's public surface (`Tool`, `ToolContext`, `EvidenceLedger`,
   `WorkerStore`, `run_check`). It must **never** import or patch `engine.py`,
   `permissions.py`, or `cli.py`'s engine paths.
2. **Implement domain tools** as `Tool` subclasses (see §3).
3. **Keep domain state in its own store** (the Sales Worker uses the existing
   `Experiment_Ledger` sqlite, additive only — it never alters core tables).
4. **Expose a thin CLI group** (`sworker <domain> ...`) that calls `SalesRepository`
   directly — it does not re-implement an engine.
5. **Register an opt-in tool list** (see `sworker/sales/tools/base.py::SALES_TOOLS`
   and `sworker/tools/__init__.py::build_registry`). A worker only receives tools
   it names; `build_registry()` excludes sales (and any other domain) tools unless
   explicitly declared. This is how `fs`-only workers are guaranteed to never see
   `sales_*`.

The contract test `tests/test_runtime_worker_contract.py` proves this works: it
runs a `ledger_analyst` (core tools only) and `sales_researcher` through the
*identical* `WorkerEngine` lifecycle and asserts **no engine code branches on
worker name**.

---

## 3. Adding a new tool (the only core extension point you typically need)

A tool is a `Tool` subclass. Risk tier + schema are the contract; the engine does
the rest.

```python
from sworker.tools.base import Tool, ToolContext, ToolResult, RiskLevel

class research_summarise(Tool):
    name = "research.summarise"
    description = "Write a cited summary of a corpus to a report file."
    risk = RiskLevel.REVERSIBLE          # or READ / EXTERNAL / FINANCIAL / DESTRUCTIVE
    input_schema = {
        "type": "object",
        "properties": {
            "corpus": {"type": "string"},
            "out":    {"type": "string"},
        },
        "required": ["corpus", "out"],
    }

    def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        # ctx.resolve() enforces the fs boundary (fs_roots); refuse to widen it.
        src = ctx.resolve(args["corpus"], must_exist=True)
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(ok=False, error=f"research.summarise: cannot read {args['corpus']!r}: {exc}")
        summary = f"# Summary\n\n{text[:500]}\n"
        dst = ctx.resolve(args["out"])
        dst.write_text(summary, encoding="utf-8")
        # Returning ok=True mints an observation; the engine records it as evidence.
        # Attach explicit evidence so the claim is independently re-verifiable.
        return ToolResult(
            ok=True,
            output=f"wrote {args['out']}",
            data={"bytes": len(summary)},
            evidence=[{"source_ref": str(src), "excerpt": text[:400]}],
        )
```

Opt the tool into the registry by appending it to your domain's tool list and
registering that list in `build_registry()` (mirroring how `SALES_TOOLS` is wired).
The worker then references `research.summarise` in its `tools:` allow-list.

**Fail-closed rules for any tool:**
- Unknown/bad input → `ToolResult(ok=False, error=...)`. The run continues; the
  failure is recorded, never swallowed.
- Never state a number you didn't derive. If you compute a figure, attach
  evidence with a real `source_ref`.
- Egress (`EXTERNAL`) and spend (`FINANCIAL`) tools must *propose*, never execute;
  the policy + `requires_approval` gate keep them behind a human.

---

## 4. Procedures (named, versioned plans)

A procedure is a YAML in `procedures/` that the planner can invoke by name (and
that the worker declares in `procedures:`). It is the auditable "how we do the
daily thing" contract — versioned and content-hashed. See `DAILY_RESEARCH.yaml`
and `DAILY_SALES_RUN.yaml` for the reference shapes, and `docs/PROCEDURES.md` (§23)
for the registry/review model.

---

## 5. Verification + separation of duties

Register domain checks with the `@check` decorator in `sworker/verify.py` (the Sales
Worker registers 5: `sales_score_recomputes`, `sales_evidence_has_source`,
`sales_outreach_approved_first`, `sales_pipeline_legal`, `sales_metrics_match_ledger`).
Each check re-derives a claim from source data independently — that is how a run
can never silently report `SUCCESS` with an unbacked number.

Enforce **separation of duties** purely through tool allow-lists: the Sales Worker
splits `sales_researcher` (discover/research/qualify, no egress) from
`sales_outreach` (draft/schedule/move, no discover). You get the same containment
for free — just don't put `external`/`financial` tools in a worker that shouldn't
send.

---

## 6. What you do NOT touch (and a guard that enforces it)

You never modify `engine.py`, `permissions.py`, `statemachine.py`, `store.py`,
`inference.py`, or the core CLI verbs (`run`/`approve`/`audit`/`replay`/`inspect`/
`why`/`verify`). If you find yourself wanting `if worker.name == "sales":` in the
engine, that is a design smell — solve it with a tool or a config field instead.

`tests/test_runtime_worker_contract.py::test_engine_has_no_domain_branching` greps
`engine.py` for exactly that pattern and **fails the build** if it appears. The
boundary is enforced by a test, not just by convention.

---

## 7. End-to-end: define → run → inspect → verify

```bash
# 1. define (a YAML, or YAML + boundary package + opt-in tools)
python -m sworker sales init --force        # installs sales worker + procedure templates

# 2. seed deterministic demo data (no private data / live APIs needed)
python -m sworker sales seed

# 3. run the full daily loop through the runtime — two worker instances,
#    one engine, external egress held for approval
python -m sworker sales daily-run --source candidates.csv --limit 3

# 4. inspect what any run actually did (reconstructable without a model)
python -m sworker inspect run_<id>

# 5. replay / audit / verify
python -m sworker replay run_<id> --mode explain
python -m sworker audit  run_<id>
python -m sworker sales verify
```

If you can do that for your domain without editing the engine, the runtime/worker
boundary is doing its job.
