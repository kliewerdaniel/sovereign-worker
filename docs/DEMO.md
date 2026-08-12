# §49 — Demo Workflow Walkthrough

A 5-minute tour that exercises the platform's core guarantees: local-only
execution, evidence derivation, the approval gate, and final verification.

---

## Scenario

A business analyst asks: **"What were total Q2 revenues?"** over a CSV of
regional quarterly revenue. The worker must compute the figure from source
data (never assert a number it didn't derive), and any external/financial
action must pass an approval gate.

---

## Steps

### 1. Initialise (one time)

```bash
export SWORKER_HOME=/tmp/demo
sworker onboard --username admin --password demo
```

Creates the workspace, an `analyst` worker, and the admin user.

### 2. Inspect the worker

```bash
sworker show analyst
```

Note the `policy` block: `external: approve`, `financial: approve`,
`destructive: approve` — the engine will pause and ask before those tiers.

### 3. Run a question

```bash
sworker run analyst "What were total Q2 revenues across all regions?"
```

The engine:
1. Classifies intent + plans steps (visible via `sworker explain analyst "…"`).
2. Executes `data.query` against `company/example.csv`, summing Q2 rows.
3. Records each step in the **append-only audit ledger**.
4. Emits an artifact with derived evidence.
5. The run finalises as `SUCCESS` (or `PARTIAL_SUCCESS` if an unbacked claim
   surfaced).

### 4. Dry-run / explain (no execution)

```bash
sworker explain analyst "What were total Q2 revenues?"
```

Returns the plan with per-step `decision` / `risk` / `reason`, and whether it
`would_require_approval` or `would_be_blocked`. **Never writes a Run.**

### 5. The approval gate (HITL)

Workers with `external/financial/destructive: approve` pause at
`AWAITING_APPROVAL`. List pending:

```bash
sworker runs --json        # find the run id + its status
sworker audit <run_id>     # see the step awaiting a decision
```

Approve via CLI or the web UI (`/run?run_id=…`). The engine resumes only after
a human decides — **the model proposes, the engine disposes.**

### 6. Verify the run

```bash
sworker verify <run_id> --json
```

Runs the run's declared verification checks (schema / set-equality / regex /
doc-section / provenance-chain). A run that skips a required check finalises as
`PARTIAL_SUCCESS`, never a silent `SUCCESS`.

### 7. Web UI

```bash
sworker web --port 8777
```

Open `http://127.0.0.1:8777/login`, sign in, and use the dashboard, run view,
and approval buttons.

---

## What this proves

| guarantee | where it's enforced |
|-----------|---------------------|
| No cloud / no model API to run | local-first core, stdlib only |
| Numbers derived, not asserted | `verify.py` + evidence + `PARTIAL_SUCCESS` degrade |
| Unapproved actions cannot auto-fire | state machine + `AWAITING_APPROVAL` gate |
| Tamper-evident history | hash-chain audit ledger (`store.verify_audit_chain`) |
| Fail-closed classification | static AST risk classifier |
| Secrets never in model context | connector resolver returns logical names only |

The `analyst` worker here is a core-tool-only worker. The same runtime also drives
the Sales Worker (`sworker sales daily-run`) — two different domains, one engine,
**zero engine code branched on worker identity** (guarded by
`tests/test_runtime_worker_contract.py`). To build a third domain without forking
the engine, see `docs/BUILDING_A_WORKER.md`.
