# Sovereign AI Worker — `sworker`

A **local-first AI worker platform**. An "AI employee" you give a YAML identity,
a set of tools, and a permission policy. It executes real work against your
local files (no cloud, no model API required to run), records every step in an
append-only audit log, and **never states a number it didn't derive** — every
figure a run produces is independently re-verified from source data.

```
REQUEST → INTENT → PLAN → ACTION → TOOL → OBSERVATION → EVIDENCE →
VERIFICATION → ARTIFACT → APPROVAL → FINAL → AUDIT
```

Verified on **Python 3.14.6** (Homebrew, `/opt/homebrew/bin/python3.14`). Core
has **zero third-party dependencies**; the only optional dep is
[Hermes Atlas](https://github.com/NousResearch/hermes-atlas) for *compiled*
company-knowledge retrieval (it degrades to labelled grep without it).

---

## Why it's built this way

- **Decomposition does not launder risk.** `DecompositionGuard` remembers a risk
  ceiling a human has already rejected or left pending in a run, and blocks any
  equal-or-higher-risk action from sneaking in afterwards — so an agent refused
  "send the email" can't get there via "write the email to a file" then
  "shell: sendmail". This guarantee only holds because risk classification
  itself is **static and fails closed**, not keyword matching:
  - `python.run` is classified by parsing the submitted code with `ast` and
    walking the real import/call graph. `subprocess`, `os.system`, `os.popen`,
    `socket`, `urllib`, `requests`, `httpx`, `ftplib`, `smtplib`, `ctypes`, … are
    EXTERNAL; `shutil.rmtree`, `os.remove`, `os.unlink`, `Path.unlink`, … are
    DESTRUCTIVE. Any `ast.parse` failure, any dynamic `eval`/`exec`/`compile`/
    `__import__`/`getattr` with a non-literal argument, or any import/call the
    walker doesn't positively recognise is escalated to the **highest tier the
    tool can reach** — it never silently defaults to the safe floor.
  - `shell.exec` resolves `argv[0]` (already allowlist-checked) and additionally
    floors interpreter binaries — `python3 -c "…"`, `bash -c "…"`, `perl`, `node`,
    `ruby`, `osascript`, … — at EXTERNAL regardless of the rest of the argv, since
    their behaviour can't be verified from a command line the way a fixed-purpose
    binary like `ls` or `cat` can.
  - See `docs/SECURITY.md` for the honest security model, its limits, and the
    fail-closed contract.
- **Nothing is fabricated.** No language model? The engine falls back to a
  deterministic plan that does real retrieval over real files and says plainly,
  in the artifact, that it ran without a model. A tool fails → the step is
  recorded as failed. Atlas missing → knowledge search degrades to labelled grep
  and the artifact says so — it never invents a claim.
- **Evidence is real.** `EvidenceLedger` mints evidence only from actual tool
  observations, each carrying a `source_ref` (file + sha256) — never model
  prose.
- **Every stated number is re-derived.** After execution, the engine turns each
  computed `data.query` figure into a `recompute_sum` verification check that
  re-sums the same source rows and compares to the derived value. A run is
  `PARTIAL_SUCCESS` if any check fails — it does not quietly keep the nicer
  number.
- **Fail-closed by construction.** Unknown input is never treated as success;
  unrecognised state escalates to the most restrictive tier; missing subsystems
  surface as `bad`/`unknown`, never `ok`. The horizon is explicit: when a
  capability (model, knowledge index, sandbox, secrets) is unavailable, the
  engine records a *degradation* and — if it loses a safety-critical guarantee —
  downgrades `SUCCESS` to `PARTIAL_SUCCESS` rather than pretending all is well.

---

## Quick start

```bash
# 1. Interpreter — Homebrew Python 3.14 (the platform runs on 3.10+, tested on 3.14.6)
/opt/homebrew/bin/python3.14 --version

# 2. Venv + install (editable, zero deps)
cd sovereign-worker
/opt/homebrew/bin/python3.14 -m venv .venv
. .venv/bin/activate
pip install -e .

# 3. Scaffold a workspace anywhere
python -m sworker init /tmp/acme
```

> **macOS env gotcha.** Hermes' shell leaks `PYTHONPATH`/`PYTHONHOME` (pointing
> at a different Python) into subprocesses. If you get `bad interpreter` /
> `SIGABRT` / import errors when launching the server, **strip both vars**:
> ```bash
> env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m sworker ...
> ```
> This is why every command below is prefixed that way.

### Seed the demo company

```bash
mkdir -p /tmp/acme/company /tmp/acme/workers
cat > /tmp/acme/company/sales.csv <<'CSV'
region,quarter,revenue,orders
North,Q1,42000,1320
North,Q2,51000,1480
South,Q1,31000,980
South,Q2,35500,1100
Online,Q1,88000,4200
Online,Q2,102000,5100
CSV

cat > /tmp/acme/workers/acme-analyst.yaml <<'YAML'
name: acme-analyst
role: Acme Coffee business analyst
instructions: |
  Compute figures from the CSVs with data.query; never state a number you did
  not derive. Write a markdown report that cites source totals.
tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]
policy:
  read: auto
  reversible: auto
  external: approve
  financial: approve
  destructive: approve
fs_roots: [company]
YAML
```

### Run a request

```bash
SWORKER_HOME=/tmp/acme \
  env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m sworker \
  run acme-analyst "What was total Q2 revenue?"
```

Output:

```
================================================================
RUN #1  SUCCESS
----------------------------------------------------------------
SUCCESS; 4 action(s) executed; 0 failed; 4 evidence item(s); 1 artifact(s);
computed: sum(revenue)=188500.0 over 3/6 rows
```

`Q2 = 51000 (North) + 35500 (South) + 102000 (Online) = 188500`. The derived
total is written into the artifact at `artifacts/q2_revenue_report.md` and
re-verified from `sales.csv` by an auto-generated `recompute_sum` check.

---

## CLI reference

Most read commands accept `--json` and emit a stable, machine-readable payload
(shared `_jprint()` helper). Most state-changing commands require an approval
gate when the policy says so.

| command | what it does |
|---|---|
| `init <home>` | scaffold a workspace (`company/`, `workers/`, `.state/`) |
| `workers` / `show <name>` | list workers / show identity + policy |
| `worker create/enable/disable/archive/clone/export/import` | worker lifecycle (§26) |
| `run <worker> "<request>"` | execute a request end-to-end (deterministic fallback if no model) |
| `runs` / `run-info <id>` / `audit <id>` / `replay <id>` | inspect runs + replay the audit trail |
| `approve <id>` / `deny <id>` / `resume <run_id>` | decide / continue a pending approval |
| `why <run_id> [--workspace]` | explain *why* a run is blocked — aggregating degradations, incidents, safe-mode, per-step notes (§65) |
| `status [--json]` | compose every hardening control into one fail-closed verdict (§66) |
| `security [--kind K] [--limit N] [--json]` | security-event ledger + audit-chain verdict (§64) |
| `safemode on/off/readonly/locked` | operator safe-mode (off / read-only / locked) (§62) |
| `incident open/close/lockdown` | incident response ledger (§63) |
| `maturity [--json]` | 10-dimension weakest-link maturity assessment (§70) |
| `benchmark [--iterations N] [--no-fail] [--json]` | regression + performance benchmarks (§58/§59) |
| `learn <run_id> <name>` / `proc` | capture a run as a versioned, diffable procedure |
| `procedure list/publish/rollback <name>` | publish / roll back a procedure (§23) |
| `template` / `trigger` / `market` | worker templates, workflow triggers (§24/§25), marketplace |
| `connectors` / `egress` / `dlp` / `message` / `browser` | execution safety surfaces (§20–§22, §54/§55) |
| `secret list/set` | optional encrypted secrets (AES-GCM, `secrets` extra) |
| `policy validate/set` / `user list/add` | auth, RBAC, policy (§4/§5) |
| `migrate [--to N] [--dry-run] [--json]` | data migrations framework (§60) |
| `doctor` | environment + config health check |
| `onboard` | guided first-run onboarding (idempotent) |
| `package` / `backup` / `export` | packaging, backup (excludes `secrets.key`), export |
| `web --port 8777` | launch the local web UI (binds `127.0.0.1` only) |
| `verify <run_id>` | run a run's verification checks |

Everything reads/writes the same local store — `sqlite` fast index +
`audit.jsonl` append-only truth under `<home>/.state/`. No separate database,
nothing leaves the machine.

---

## Web UI

```bash
SWORKER_HOME=/tmp/acme \
  env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m sworker web --port 8777
# open http://127.0.0.1:8777
```

On startup the server prints a **per-session token** to stdout:

```
Sovereign AI Worker UI on http://127.0.0.1:8777  (Ctrl-C to stop)
session token: A1d2IzydUVOo5C5nxi8lWWBP1lwQCIKHd_sLStVHhDs
(pass it as ?token=... on state-changing requests, or X-SW-Token header)
```

Binding to `127.0.0.1` is **not** a CSRF defense — any page in the same browser
could otherwise POST to `/approve` or `/resume` with no token. So every
state-changing request (`/run`, `/approve`, `/deny`, `/resume`, `/verify`)
requires that token (as `?token=` or the `X-SW-Token` header) **and** a
same-origin `Origin`/`Referer`. Requests failing either check get `403`. The
token is embedded in the page's forms automatically, so clicking buttons in the
UI needs nothing extra; only direct/scripted POSTs must supply it. Pass
`--token <fixed>` to use a stable token (e.g. behind a reverse proxy that already
enforces auth).

A single-file `http.server` app (stdlib only) that lets you:

- submit a request to a worker and watch the run appear;
- replay a run's audit trail, evidence (with `source_ref`s), and artifacts;
- **approve / reject a pending approval and resume the run** — the full gate loop
  over HTTP;
- run a run's verification checks;
- inspect the hardening dashboards: **`/dashboard`**, **`/security`** (events +
  audit-chain verdict), **`/status`** (composed verdict), **`/maturity`**,
  **`/procedures`** (metadata-only review ledger), **`/why`** (block explainer).

JSON API (versioned, self-describing via `/api/v1/openapi.json`):
`GET /api/v1/{health,workers,runs,metrics,dashboard,security,status,maturity,
safemode,procedures,why,incident,explain}` plus `GET /api/runs`, `/api/egress`,
`/api/dlp`. The web UI also serves the legacy `GET /api/runs`.

---

## Tests

Real integration tests — they build a temp workspace, seed CSV data, run the
engine **without a language model** (deterministic fallback), and assert on the
persisted run/evidence/artifact/verification records. No mocks, no cloud.

```bash
cd sovereign-worker
env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/ -q
# 443 passed
```

Coverage spans every subsystem (engine lifecycle, state machine, tenant
isolation, auth/RBAC/policy, audit hash-chain, evidence minting, verification,
prompt-injection scanning, safe mode, incident response, degradation ledger,
security events, block explainer, system status, maturity, benchmarks,
migrations, connectors/egress/DLP/messaging, worker lifecycle, web UI, and the
§68 end-to-end integration suite). The suite is the contract — it is run after
every change and must stay green.

### Operational notes (so you don't re-hit known gotchas)

1. **`/tmp` symlink escapes the sandbox.** On macOS `/tmp` → `/private/tmp`.
   `os.path.relpath` produced `../../private/tmp/...` which the verification
   path guard rejected as "escapes workspace". Fix: `realpath` *both* sides
   before `relpath`.
2. **Derived `data.query` figures lost their filter.** The verification spec was
   built from `data.query`'s `data` dict, which omits `where`/`group_by`, so the
   recompute summed *all* rows → `FAIL`. Fix: thread the original tool `args`
   into the computed record and prefer `args["where"]`/`args["group_by"]`.
3. **Web-auth rewrite broke `POST /run`.** During the §62 hardening the
   `elif url.path == "/run":` branch header was lost, leaving run-handling code
   mis-indented inside the `/api/v1/incident` block — every `POST /run` returned
   `404`. Restored the branch header. (Live-tested before the fix was merged.)

---

## Architecture

```
sworker/
  config.py          Workspace + WorkerConfig (policy, fs_roots, timeouts, resource limits)
  models.py          RunStatus (11-state) / ActionStatus / StepStatus / RiskLevel /
                     Provenance / VerificationOutcome / Record / Task / Plan / Run
  store.py           WorkerStore: sqlite index + JSONL audit (reconstructable runs)
                     + audit hash-chain + degradations + sessions + tenants
  engine.py          WorkerEngine lifecycle + deterministic fallback + verification +
                     resource budgets + watchdog + graceful-degradation hooks
  statemachine.py    11-state run lifecycle (rejects illegal transitions)
  org.py             tenant isolation (cross-tenant access is a hard error)
  auth.py            AuthProvider (stdlib PBKDF2-HMAC-SHA256), sessions, revocation
  rbac.py            role ladder + least-privilege enforcement
  policy.py          immutable policy + risk floor
  permissions.py     PermissionEngine (AST risk classifier, fail-closed)
  approvals.py       ApprovalManager (immutable approve/reject, HITL quorum/escalation)
  evidence.py        EvidenceLedger (mint from real observations only)
  verify.py          deterministic checks: recompute_sum/delta/row_count/
                     file_exists/artifact_contains_evidence/totals_match_source
  procedures.py      learn_from_run -> versioned, diffable YAML procedures + publish/rollback
  scheduler.py       parse_cron / next_fire
  knowledge.py       Hermes Atlas bridge (compile company/*.md -> claim retrieval)
  connectors.py      default-deny connector registry (SSRF-guarded egress)
  dlp.py             data-loss-prevention primitives (opt-in rules)
  injection.py       prompt-injection scanner (static rule families)
  safemode.py        operator safe mode (off / read-only / locked)
  incident.py        incident response ledger
  degradation.py     graceful-degradation ledger (warn / critical)
  security_events.py security-event catalog + dashboard payload
  block_explainer.py "why blocked?" aggregation (§65)
  system_status.py   composable system-status surface (§66)
  maturity.py        10-dimension weakest-link maturity model (§70)
  benchmark.py       regression + performance benchmark harness (§58/§59)
  migrations.py      data migration framework (fail-closed on downgrade) (§60)
  secrets.py         optional AES-GCM encrypted secrets (secrets extra)
  lifecycle.py       worker lifecycle (enable/disable/archive/export/import/clone)
  trigger.py         workflow triggers (file_changed / webhook / event)
  templates.py       worker templates + marketplace
  metrics.py         run/action metrics
  logging.py         structured, redacting logger
  doctor.py          environment + config health check
  package.py         packaging + backup/export
  inference.py       model interface (NullInference fallback when no model)
  web.py / web_main.py  local-first web UI + versioned JSON API
  cli.py             command line (all subcommands above)
  sales/             Sales Worker boundary layer (reference impl of the
                     runtime/worker boundary — adds a 2nd domain via WorkerConfig
                     + opt-in tools, NOT engine forks)
    base.py          Tool / ToolContext / risk floor / subprocess tracking
    fs.py            file read/list/write (root-bounded)
    exec.py          shell.exec + python analysis (resource-timeout bounded)
    data.py          query / inspect (CSV + derived verifications)
    http.py          http.get / http.post (network-category, egress-guarded)
    git.py           git operations (egress-guarded)
    browser.py       browser tool (allowlist + timeout)
    message.py       messaging (allowlist + rate-limit)
    knowledge.py     knowledge.search (Atlas bridge)
    sandbox.py       execution isolation (none / docker)
```

**Design commitments:** local-first, no cloud APIs; the model proposes and the
engine disposes; nothing fabricated; verification re-derives from source with no
model in the loop; the store is sqlite fast-index + JSONL truth so any run is
byte-for-byte reconstructable; fail-closed everywhere — unknown ≠ success.

---

## Security

The honest security model — permission/risk classification (and its fail-closed
behaviour), the execution sandbox limits, HTTP SSRF surface, git egress,
prompt-injection scanning, safe mode, incident response, the degradation
contract, the web UI's token + same-origin CSRF defense — is documented across
the `docs/` set below. Start with [`docs/SECURITY.md`](docs/SECURITY.md) before
deploying a worker that can reach the network or push to a remote, then
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) and
[`docs/TRUST_BOUNDARY.md`](docs/TRUST_BOUNDARY.md).

---

## Documentation index

| doc | what it covers |
|---|---|
| `docs/ROADMAP.md` | the §-numbered plan + progress tracker (all phases complete; 443 passed) |
| `docs/ARCHITECTURE.md` | system architecture overview + cross-links to every subsystem |
| `docs/SECURITY.md` | honest security model, limits, fail-closed contract |
| `docs/THREAT_MODEL.md` | 10 adversary classes mapped to real modules/symbols/tests (§51) |
| `docs/TRUST_BOUNDARY.md` | trust boundaries + where authority actually lives (§43) |
| `docs/GRACEFUL_DEGRADATION.md` | degradation ledger: warn/critical, success downgrade (§61) |
| `docs/SAFE_MODE.md` | operator safe mode levels + fail-closed invariants (§62) |
| `docs/INCIDENT_RESPONSE.md` | incident ledger + response runbook (§63) |
| `docs/SECURITY_EVENTS.md` | security-event catalog + dashboard payload (§64) |
| `docs/WHY_BLOCKED.md` | "why blocked?" explainer aggregation (§65) |
| `docs/SYSTEM_STATUS.md` | composable system-status surface (§66) |
| `docs/MATURITY.md` | 10-dimension maturity model (§70) |
| `docs/BENCHMARKS.md` | regression + performance benchmark harness (§58/§59) |
| `docs/PROCEDURES.md` | procedure publish/rollback + web review ledger (§23) |
| `docs/INTEGRATION_TESTS.md` | end-to-end integration test suite (§68) |
| `docs/OPERATIONS.md` | operations runbook + deployment (Docker) |
| `docs/SALES_INTEGRATION.md` | Sales Worker integration design + the runtime/worker boundary proof |
| `docs/BUILDING_A_WORKER.md` | how to add a 3rd domain worker without touching the engine |
| `docs/DEMO.md` | demo walkthrough |

---

## License

MIT. See `LICENSE`. Core has **zero third-party runtime dependencies**; optional
extras (`secrets`, `ingest-pdf`, `ingest-docx`, `atlas`) degrade gracefully when
absent.
