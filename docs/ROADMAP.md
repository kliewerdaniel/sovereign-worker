# Sovereign Worker — Client-Ready Platform Evolution: Plan

This document is the **internal implementation plan** required by the productization
spec (§1 "understand the existing system first", §71 "work incrementally"). It is a
living plan: each phase closes with a commit of coherent, tested changes.

Guiding rule from the spec, honored throughout: **do not make one enormous untestable
rewrite.** Every change lands as a small, tested, reviewable increment.

---

## 0. Baseline (established 2026-08-11)

* Working tree clean; HEAD `b8fc967`; **60 tests pass** on Python 3.14.6.
* Core has **zero third-party dependencies** (stdlib only; Hermes Atlas optional).
* Interpreter invocation (every command/test):
  `env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14`
* Test suite: `cd sovereign-worker && env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/ -q`

## 1. Architecture map (what exists)

```
sworker/
  config.py      Workspace + WorkerConfig (5-key risk policy, fs_roots, timeout)
  models.py      Enums (RunStatus, ActionStatus, StepStatus, RiskLevel, Provenance,
                Confidence, VerificationOutcome, ApprovalState) + Records
                (Task/Plan/Step/Action/Observation/Evidence/Claim/Verification/
                Approval/Artifact/Run)
  store.py       WorkerStore: sqlite fast-index + JSONL append-only audit truth
  tools/         base (Tool/ToolContext/Registry), fs, exec(shell+python), http,
                git, browser(port), message(port), data(csv), knowledge(atlas bridge)
  permissions.py PermissionEngine + DecompositionGuard (AST static classify, FAIL CLOSED)
  approvals.py   ApprovalManager (immutable approve/reject records)
  evidence.py    EvidenceLedger (mints ONLY from real observations / compiled claims)
  verify.py      deterministic checks (recompute_sum/delta/row_count/file_exists/
                artifact_contains_evidence/totals_match_source)
  engine.py      WorkerEngine lifecycle + deterministic fallback + verification
  procedures.py  learn_from_run -> versioned diffable YAML; substitute/expand
  scheduler.py   parse_cron / next_fire (Vixie semantics, dow translation)
  knowledge.py   Hermes Atlas bridge (compile company/*.md -> claim retrieval, grep fallback)
  inference.py   local-first LLM port (refuses remote unless SWORKER_ALLOW_EXTERNAL=1)
  web.py         stdlib http.server UI (functional: run/approve/resume/verify + token CSRF)
  cli.py         command line
```

### Invariants to preserve (do NOT break)

1. **Model proposes, engine disposes.** Every action routes through
   `PermissionEngine.evaluate` before it runs; the model cannot see/alter the decision.
2. **Evidence is real.** `EvidenceLedger` mints only from actual observations /
   compiled-knowledge records — never from model prose.
3. **Verification re-derives from source**, no model in the loop.
4. **Store = sqlite index + JSONL truth**; a run is reconstructable from the ledger.
5. **Deterministic fallback**: the engine runs end-to-end with *no LLM* (no fabrication).
6. **Tool boundary** via `ToolContext.resolve()` (realpath both sides; no path escapes).
7. **Zero third-party deps in core.**
8. **Fail-closed permission classification** (AST, not keyword matching).

### Existing seams (good extension points — do not reinvent)

* `Tool` / `ToolContext` port: `validate()` schema check + `resolve()` boundary.
  → use for **capability negotiation** (§7) and **resource limits** (§10).
* `BrowserBackend` / `MessageBackend` Protocol ports (`NullBrowser`, `OutboxBackend`).
  → use for **connectors** (§20), **browser hardening** (§21), **messaging policy** (§22).
* `knowledge.py` KnowledgeProvider (Atlas compile/retrieve). → **Atlas deepening** (§17).
* `inference.py` local-first port. → **inference abstraction** (§39) + **failure handling** (§40).
* `PermissionEngine` / `DecompositionGuard` = the **policy engine core** (§6).
* `store.audit()` append-only. → **hash chain** (§13) + **security events** (§64).
* `verify.py` check registry. → **generalized verification framework** (§15).

### Identified gaps vs spec (by phase)

*Phase 1 (Foundation):* no org/workspace/tenant id on records; no user auth; no RBAC;
policy is only a 5-key risk map (no versioned/immutable fs/network/tool grants); no
secrets; audit has no hash chain / `audit verify`; run state machine has states but no
enforced transitions or transition log; no cancellation/CANCELLED; resource limits are
only timeout/max_output (no max_runtime/actions/tool_calls/artifact_size, no safe kill).

*Phase 2 (Knowledge):* verification only numeric; no provenance chains; artifacts don't
expose claims; Atlas has no indexing status/stale/rebuild/sync; no ingestion adapters
(.pdf/.docx/.json); no sync watcher.

*Phase 3 (Execution):* no connector architecture; browser needs allowlists/credential
isolation; messaging needs draft→policy→approval→receipt; no improved python/shell
isolation beyond subprocess; no network egress registry / default-deny.

*Phase 4 (Automation):* procedures lack publish/run/rollback/permissions; no triggers
(file_changed/webhook/event); no worker templates; no worker lifecycle (clone/enable/
disable/archive/export/import/version).

*Phase 5 (Product):* web UI lacks dashboard/worker/run/approval pages + explainability;
no `doctor`; no backup/restore; no export/import package; docs are only README+SECURITY.

*Phase 6 (Hardening):* no adversarial suite (cross-tenant/prompt-injection/audit-tamper/
replay/malicious-files); no threat model doc; no perf benchmarks; no migration framework;
no maturity model; no safe mode / incident response / "why blocked?" / security events.

---

## 2. Phased plan (priority order per spec §72)

Each phase is a sequence of small, tested, committed increments. Phase boundaries are
not hard walls — later phases may pull a foundation piece forward if it unblocks a demo.

### Phase 1 — Foundation (this plan's immediate focus)
1. **Organization + Workspace + tenant isolation** (§3). `org_id`/`workspace_id` stamped
   on every record; `WorkspaceRegistry`; cross-workspace access **fails closed** (tests).
2. **Local authentication** (§4). `auth.py`: users, argon2/scrypt password hash (stdlib
   `hashlib`+`secrets`; no new dep), sessions (opaque tokens), logout, expiry,
   revocation, password change, disablement. OAuth/OIDC-shaped for later.
3. **RBAC** (§5). `rbac.py`: roles Owner/Admin/Operator/Approver/Viewer; granular perms
   (`workers.read`, `runs.execute`, `approvals.grant`, …); **enforced server-side**
   (engine + web + cli), never UI-only.
4. **Policy engine** (§6). Extend `permissions.py` into a versioned, immutable policy
   object (fs read/write paths, network allow, tool allow, risk map); runs record the
   exact policy version that governed them; export/import/diff.
5. **Secrets** (§8). `secrets.py`: encrypted-at-rest store (key from env / OS keychain
   abstraction), redaction, access policy; never in prompts/logs/audit/artifacts.
6. **Hash-chain audit** (§13). Wrapper over `store.audit` adding `event_id`,
   `previous_event_hash`, `event_hash`; `audit verify` command checks the whole chain.
7. **Run state machine** (§12). Formal states (CREATED/PLANNING/WAITING_FOR_APPROVAL/
   EXECUTING/VERIFYING/COMPLETED/PARTIAL_SUCCESS/FAILED/CANCELLED/DENIED); illegal
   transitions impossible; every transition persisted with previous_state/actor/reason.
8. **Cancellation** (§11). `cancel(run_id, by, reason)`; propagates to child processes
   (process groups), records CANCELLED + who/when/current-action/reason.
9. **Resource controls** (§10). `max_runtime/max_actions/max_tool_calls/max_output_bytes/
   max_artifact_size/max_python_runtime/max_shell_runtime/max_network_requests`; engine
   terminates runaway execution safely; exhaustion recorded as structured failure.

### Phase 2 — Knowledge
Generalized verification framework (§15: schema/regex/set-equality/doc-section/deterministic
script checks); claim-level provenance + artifact claim exposure (§16); Atlas deepening
(index status, stale detection, rebuild, incremental) (§17); ingestion adapters
(.md/.txt/.csv/.json/.pdf/.docx) (§18); sync watcher (§19).

### Phase 3 — Execution
Connector architecture + 2-3 high-value connectors (§20); browser hardening: allowlists,
download/upload/credential/session isolation, timeouts (§21); messaging draft→policy→
approval→send→receipt (§22); improved python/shell isolation abstraction (§9); network
egress registry + default-deny + UI visibility (§54); DLP primitives (§55).

### §20 connector architecture — COMPLETE
- `connectors.py` (new, zero-dep):
  - `Connector` Protocol + `ConnectorBase` (default-deny scaffolding with regex
    `allow` lists and `secret_refs` → §8 store).
  - Built-in connectors: `HttpConnector` (delegates to `tools/http.py`, enforces
    host allow-list, resolves token secret ref), `SlackConnector` (delegates to
    `tools/message.py` outbox, enforces channel allow-list + token ref).
  - `ConnectorManager` — the default-deny chokepoint. **Nothing** is reachable
    unless the worker explicitly declares a connector; an enabled connector
    permits **nothing** until its allow-list matches. `authorize(kind, action,
    target)` returns `(ok, reason, conn)`; `resolve_credentials(kind)` resolves
    secret refs to plaintext at call time, **fail-closed**: a missing ref or
    unavailable store raises `ConnectorError` (never an anonymous authorized call).
    Credential *values* are never returned to the caller — only logical names in
    `connector_action`'s `credentials_used`.
- `config.py`: `WorkerConfig.connectors: List[Dict]` added (persisted in `to_dict`/
  `from_dict`); `connectors` is the single place a worker declares external access.
- `engine.py`: builds a `ConnectorManager` from the worker's `connectors` (with a
  lazy `_resolve_secret` wired to the §8 store), and exposes `connector_action()` —
  the only path a worker reaches an external system. Returns a transport descriptor
  (what *would* run), never the secret value.
- `cli.py`: `sworker connectors list <worker>` (shows enabled connectors + allow
  lists + credential names, no secrets) and `connectors check <worker> <kind>
  <target>` (ALLOWED/REFUSED with reason, exit 1 on refusal).
- Tests: `tests/test_connectors.py` (9) — default-deny when no specs; empty
  allow-list refuses all; allow-list match/refuse; unknown kind rejected; slack
  channel allow; credential injection (values not refs); missing-secret fail-closed;
  required-creds-without-resolver refuse; `describe` never leaks secrets.
- Verified live: `init` + worker declaring `slack` connector → `connectors list`
  shows `allow=['^general$'] credentials=['token']`; `connectors check` on `#general`
  refused only because the secret ref was absent (correct fail-closed), `#secret`
  refused by allow-list; both exit 1.

### §21 browser hardening — COMPLETE
- `config.py`: worker browser policy fields (all default-deny): `browser_allow`
  (regex URL allow-list), `browser_timeout` (per-open cap, default 30s),
  `browser_downloads` (default `False`), `browser_uploads` (default `False`),
  `browser_credential_refs` (secret refs injected at call time),
  `browser_private_session` (default `True`). Persisted in `to_dict`/`load_worker`.
- `tools/base.py`: `ToolContext` carries the browser policy + `secret_resolver`
  (the §8 resolver the engine wires in) so a tool can never widen its own boundary.
- `tools/browser.py` (rewritten, still zero-dep, still driver-free):
  - Port extended with `download(url, dest)`, `set_credentials(creds)`,
    `set_private_session(private)` so any plugged-in backend honors isolation.
  - `BrowserOpen` enforces URL allow-list (default-deny; also rejects
    `file://`/`javascript:` and other non-web schemes) and caps the timeout to
    `ctx.browser_timeout` before the backend is touched. Applies session +
    credential isolation to the backend before navigating; never returns secret
    *values* (only the ref names in `credentials_injected`).
  - New `BrowserDownload` (default-deny; destination confined to the fs boundary
    via `ctx.resolve`, downloads only when `browser_downloads` is set) and
    `BrowserUpload` (default-deny; source confined to fs boundary + must exist when
    `browser_uploads` is set). Both fail-closed.
- `engine.py`: `_tool_ctx` now fills every §21 field from `WorkerConfig` and
  wires `secret_resolver=self._resolve_secret` (same fail-closed resolver as §20).
- `cli.py`: `sworker browser policy <worker>` prints the worker's §21 posture
  (allow-list, timeout, downloads/uploads, credential refs, private session).
- Tests: `tests/test_browser_hardening.py` (14) — empty allow-list denies all;
  non-http schemes refused; open permitted/refused; timeout capped to worker
  ceiling; download denied-by-default + permitted-confined; upload denied-by-default
  + permitted-but-confined + rejects path outside boundary; credentials injected
  but values never returned; missing credential refuses; creds-without-resolver
  refuses; private session isolated by default.
- Verified live: `init` + worker with `browser_allow`/`browser_downloads`/`creds`
  → `browser policy` shows the posture; harness confirms backend never touched
  when refused.

### §22 messaging policy — COMPLETE
- `config.py`: worker messaging policy (default-deny channel) — `message_allow`
  (regex channel allow-list), `message_rate_limit` (per-run cap; `0` = bounded
  only by `max_actions`). Persisted in `to_dict`/`load_worker`.
- `tools/base.py`: `ToolContext` carries `message_allow`, `message_rate_limit`,
  and a shared `messages_sent` counter (incremented by the tool on delivery, read
  by the rate-limit guard) so the policy is enforced without the engine knowing
  the tool's internals.
- `tools/message.py` (rewritten, still zero-dep, still outbox-backed):
  - Channel allow-list (default-deny) enforced for BOTH draft and send before
    anything is written.
  - Rate limit: once `messages_sent >= message_rate_limit` (when > 0), delivery
    is refused. Drafts do not consume the budget.
  - Draft mode: `message.send draft=true` writes the message to the outbox with
    `delivered: False` and reports "drafted (not delivered)" — composes without
    egress and without needing approval to *draft*; actual delivery still
    requires approval (`message.send` remains `requires_approval=True`).
  - Receipt: every send/draft returns a structured receipt (`receipt_id`,
    `channel`, `delivered`, `ts`, `backend`) for auditability.
  - Backend port now takes a `delivered` flag so any real (e.g. Slack) adapter
    receives the same already-policy-checked, already-draft-aware call.
- `engine.py`: `_tool_ctx` fills `message_allow` + `message_rate_limit`.
- `cli.py`: `sworker message policy <worker>` prints the §22 posture
  (channel allow-list, rate limit, approval requirement, draft support).
- Tests: `tests/test_messaging_policy.py` (8) — empty allow-list denies all;
  channel normalized (`#general`→`general`); send permitted/refused; draft
  composes without egress + without consuming budget; rate limit blocks after
  cap (and only delivered messages count); drafts ignored by rate cap; receipt
  structured; draft still denied on disallowed channel.
- Verified live: `init` + worker with `message_allow`/`message_rate_limit: 3`
  → `message policy` shows the posture.

### §9 execution isolation abstraction — COMPLETE
- `sworker/tools/sandbox.py` (new): single `Sandbox` boundary that BOTH
  `shell.exec` and `python.run` route through (via `run_in_sandbox`). Owns the
  isolation primitives — env allow-list, fs boundary (`cwd`), timeout, and
  whole-process-group kill on cancel/timeout — so they cannot drift between the
  two tools.
  - Backends: `none` (host subprocess; default, no deps) and `docker` (wraps the
    command in `docker run --rm --network none -w /work -v <workspace>:/work:rw
    --user 65534:65534 --cap-drop ALL`). Docker is invoked via the host CLI; no
    third-party dependency.
  - **FAILS CLOSED**: requesting `sandbox: docker` when the `docker` CLI is
    unavailable raises `SandboxError` and the tool returns `ok=False` with the
    reason in `data.sandbox_error` — it NEVER silently downgrades to host
    execution (the exact failure mode that quietly weakens isolation).
  - Unknown `sandbox` mode is rejected at construction (`SandboxError`).
  - `RunResult` carries the `sandbox` mode so every observation records which
    boundary actually executed the command.
- `config.py`: `WorkerConfig.sandbox` ("none" | "docker"), persisted via
  `to_dict`/`load_worker`.
- `tools/base.py`: `ToolContext.sandbox` (engine fills from the worker).
- `tools/exec.py`: rewritten so the tools do *policy* (argv allow-list,
  schema) and delegate *isolation* to `Sandbox`. `python.run` still captures
  `RESULT_JSON:` and emits evidence; `data.sandbox` records the boundary used.
- `engine.py`: `_tool_ctx` fills `sandbox`.
- Tests: `tests/test_isolation.py` (7) — unknown mode rejected; shell/python run
  inside `none` on host and capture `RESULT_JSON:` evidence; `docker` raises
  `SandboxError` when the binary is absent; `shell.exec sandbox:docker` does NOT
  run on host and reports `sandbox refused to run`; `run_in_sandbox` reports the
  mode; docker argv wrapping shape when docker present. `test_cancel.py` updated
  to the `run_in_sandbox` API (cancel still kills the group).
- Verified live (tool-level): `none` executes on the host; `docker` invokes the
  real `docker run` wrapper (daemon-down returns exit 125, NOT host execution);
  absent-binary path raises `SandboxError` (fail-closed) under test.

### §54 network egress registry — COMPLETE
- `config.py`: `WorkerConfig.egress_allow` (regex host allow-list; **empty = deny
  all egress**), persisted via `to_dict`/`load_worker`.
- `tools/base.py`: `ToolContext.egress_allow` (engine fills from worker).
- `tools/http.py` (rewritten): every outbound request is checked against the
  default-deny `egress_allow` list via `_check_egress` BEFORE any network
  contact. A refusal returns `ok=False` with `data.refused=True` + `data.reason`
  and never calls `urlopen`. Defense-in-depth SSRF guard (`_ssrf_blocked`) blocks
  cloud-metadata / link-local / private ranges REGARDLESS of the allow-list, and
  non-http(s) schemes (`file:`, `gopher:`, …) are refused. Both the decision and
  destination are recorded on the observation (`data.url` / `data.egress` /
  `data.refused` / `data.reason`) for UI visibility.
- `engine.py`: `_tool_ctx` fills `egress_allow`.
- `cli.py`: `sworker egress policy <worker>` prints the §54 posture (host
  allow-list + SSRF guard note).
- `web.py`: index worker cards show each worker's egress allow-list; new
  `GET /api/egress` (JSON) returns every observation that touched the network
  boundary, split into `allowed` and `refused` (URL, status, reason) — sourced
  from stored observations, no live contact. `render_egress_log` lives in
  `tools/http.py` (no web coupling) and is imported by the route.
- Tests: `tests/test_egress.py` (9) — empty allow-list denies all; host regex
  allow/deny; SSRF blocklist (metadata/loopback/private) always blocks; SSRF
  blocked even when a pattern would match; non-http scheme refused; refused GET
  records reason and never contacts the network; allowed GET egresses and tags
  `data.egress`; `render_egress_log` splits allowed vs refused from the store.
- Verified live: `init` + worker with `egress_allow: ['^api\\.public\\.example\\.com$']`
  → `egress policy` shows the posture.

### §55 DLP primitives — COMPLETE
- `dlp.py` (new, zero-dep): `DlpPolicy` compiles a worker's named `dlp_rules`
  against `BUILTIN_DLP_RULES`. `scan(text)` returns the first `DlpHit` (rule +
  kind) or `None`. Operator-intent-required: `dlp_rules` is OPT-IN — an empty
  list means no scanning at all (no silent built-in scanning), consistent with
  the other default-deny subsystems. An unknown rule name fails closed
  (`KeyError`) at policy build time. The matched text is **never** returned —
  only the rule name + human kind — so an observation can say *what kind* of
  secret was stopped without leaking it.
- Built-in catalog `BUILTIN_DLP_RULES`: `aws_access_key_id`, `private_key_block`,
  `api_token`, `email_address`, `us_ssn`, `credit_card`.
- Enforcement at the egress boundary, fail-closed (refuse BEFORE any bytes
  leave the machine):
  - `tools/http.py`: `_request` scans the URL (GET) and the JSON body (POST)
    via `DlpPolicy(ctx.dlp_rules)`; a hit returns `ok=False` with
    `data.dlp_blocked=True` + `rule` + `kind` and never calls `urlopen`.
  - `tools/message.py`: `SendMessage` scans the message text (drafts included —
    a drafted secret is still a leak waiting to send) before the backend; a hit
    returns `ok=False` with the same `dlp_blocked`/`rule`/`kind` shape and never
    reaches the backend.
- `config.py`: `WorkerConfig.dlp_rules` (list of catalog names; empty = no
  scanning), persisted via `to_dict`/`load_worker`. `tools/base.py`:
  `ToolContext.dlp_rules`. `engine.py`: `_tool_ctx` fills it.
- `cli.py`: `sworker dlp policy <worker>` prints the §55 posture (active rules +
  catalog). `web.py`: index worker cards show each worker's `dlp_rules`; new
  `GET /api/dlp` returns every stored observation blocked by DLP (`rule`/`kind`,
  never the payload) via `render_dlp_log` (in `dlp.py`, no web coupling).
- Tests: `tests/test_dlp.py` (11) — unknown rule fails closed; known rule scans;
  empty rules = no scan; POST body with AWS key blocked (secret never in error);
  clean POST passes; GET URL with SSN blocked; message with private key blocked
  (never reaches backend); clean message passes; `render_dlp_log` surfaces
  blocked observations without the payload; catalog present.
- Verified live: `init` + `leakguard` worker with `dlp_rules: [aws_access_key_id,
  private_key_block]` → `dlp policy` shows posture.

### Phase 3 — DONE
- §20, §21, §22, §9, §54, §55 all complete.

### Phase 4 — Automation

Procedure publish/run/rollback + permissions (§23); workflow triggers (§24); worker
templates/marketplace (§25); worker lifecycle (§26).

#### §23 Procedure publish / run / rollback + permissions — COMPLETE
- `procedures.py`: `publish_procedure(worker, name, path, *, role, by)` (fail-closed
  version bump `1.0`→`1.1`; rejects clobber unless minor bump), `rollback_procedure`
  (min 2 versions; refuses to roll back the only published version), `list_published`,
  `current_version`, `can_publish(rbac, role)`.
- `rbac.py`: added `procedure:publish` permission, granted to operator/owner roles
  (viewer denied → CLI exits 3).
- `cli.py`: `procedure` subparser — `list` / `publish <worker> <name> <path>` / `rollback
  <worker> <name> [--version]`.
- `tests/test_procedures_publish.py` (10). Suite → 230.
- **Web review ledger added (later):** `sworker/web.py` `render_procedures` +
  `GET /procedures` (per-worker table of published versions: name / version /
  current pin / author / SHA-256) and `GET /api/v1/procedures` JSON mirror
  (`{"procedures":[...]}`). The ledger shows only metadata — procedure bodies
  are never inlined on the page or API. Nav-linked from the status/security bar.
  `tests/test_web_procedures.py` (2) covers both surfaces and asserts no body
  leak. Doc `docs/PROCEDURES.md`; cross-linked `ARCHITECTURE.md`.

#### §24 Workflow triggers — COMPLETE
- `trigger.py` (new, zero-dep): `normalize_trigger` / `resolve_triggers` (fail-closed:
  unknown kind, relative/absent `roots`, missing webhook `path` all rejected);
  `FileWatcher` (stdlib polling, primed-baseline so first snapshot is not a false fire);
  `EventBus` (in-process named pub/sub); `WebhookReceiver` (stdlib `http.server`,
  routes POST by path, `X-Sworker-Secret` gate, 404 unknown / 405 wrong method / 403
  bad secret).
- `config.py`: `WorkerConfig.triggers: List[Dict]` persisted in `to_dict`/`load_worker`.
- `cli.py`: `trigger validate|watch|serve <worker>` (serve binds a stdlib HTTP receiver;
  watch runs the FileWatcher daemon).
- `tests/test_triggers.py` (7): validation rejects, file watcher detects new file, event
  bus dispatch, webhook routing + 404/405/403 gates. Suite → 237.

#### §25 Worker templates / marketplace — COMPLETE
- `templates.py` (new, zero-dep): built-in `analyst` / `operator` / `ingest` templates
  (with `{name}`/`{goal}` placeholders, ingest wires a `file_changed` trigger); `create_worker`
  validates rendered YAML before writing and refuses to clobber (fail closed); optional
  marketplace = `workers/marketplace/*.yaml` with `list_marketplace` / `publish_to_marketplace`
  / `import_from_marketplace` (reuses §26 importer).
- `cli.py`: `template list|create <template> <name> [--goal]|market {list,publish,import}`.
- `tests/test_templates.py` (6): unknown template → error, create-from-template, no-clobber,
  marketplace publish/import round-trip, duplicate publish rejected. Suite → 243.

#### §26 Worker lifecycle — COMPLETE
- `lifecycle.py` (new, zero-dep): `set_enabled` (flip `disabled`; engine refuses to run
  disabled workers), `clone` (no-clobber unless `--force`), `archive` (relocate to
  `workers/archived/`, never delete), `export_worker` (portable YAML, `path` stripped),
  `import_worker` (no-clobber unless `--force`), `list_versions` (sha256 snapshots under
  `workers/versions/<name>/` recorded before every mutation).
- `config.py`: `WorkerConfig.disabled: bool` persisted; `engine.py run()` refuses disabled
  workers at the boundary.
- `cli.py`: `worker enable|disable|clone|archive|export|import|versions <…>`.
- `tests/test_worker_lifecycle.py` (7): disable/enable, no-clobber clone, archive relocate-
  not-delete, export/import round-trip (path stripped), version snapshots, engine refuses
  disabled worker. Suite → 250.

### Phase 4 — DONE
- §23, §24, §25, §26 all complete. Full suite: **250 passed**.

### Phase 5 — Product
Admin dashboard (§27), explainability (§28), dry-run (§29), replay distinction (§30),
export/import package (§31), backup/restore (§32), `doctor` (§33), observability/metrics
(§34), structured logging + redaction (§35), web/API security hardening (§36), `/api/v1`
versioning + OpenAPI (§37), CLI coherence + `--json` (§38), onboarding flow (§46),
deployment modes + Docker (§47), reference business deployment (§48), demo workflow (§49),
professional docs set (§50).

#### §36 Web/API security hardening — COMPLETE
- `sworker/web.py`: new `_send_json()` emits hardening headers on every JSON response —
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'; base-uri 'none'`, `Cache-Control: no-store`. No secret values
  are ever reflected into API bodies (fail-closed: callers own omission).
- All pre-existing JSON routes (`/api/runs`, `/api/egress`, `/api/dlp`) now flow through
  the same hardened path.

#### §37 `/api/v1` versioned API + OpenAPI — COMPLETE
- `sworker/web.py`: new `/api/v1/*` subtree behind the session gate:
  `GET /api/v1/health` (§33 doctor), `GET /api/v1/workers` (§26), `GET /api/v1/runs`,
  `GET /api/v1/runs/{id}` (404 on miss), `GET /api/v1/metrics` (§34),
  `GET /api/v1/openapi.json` (self-describing OpenAPI 3.0 doc),
  `POST /api/v1/explain` (§29 — builds an engine, returns the plan, **never creates a Run**).
- All routes require a valid session cookie (fail-closed: 401/403 without one) and carry
  the §36 hardening headers.
- `tests/test_web.py`: 4 new tests — unauthenticated API rejected, health/workers/metrics
  shape + CSP/nosniff headers, OpenAPI doc structure, run-detail 404 + explain POST no-run.

#### §38 CLI `--json` coherence — COMPLETE
- `sworker/cli.py`: every read/inspection command now emits a structured view via a
  shared `_jprint()` helper when `--json` is passed: `workers`, `show`, `runs`,
  `run-info`, `audit`, `proc`, `sched list`, `verify`, `connectors`, `browser`,
  `message`, `egress`, `dlp`, `procedure list`, `user list`, `policy`, `knowledge status`.
  Without `--json` the human-readable output is unchanged. `--json` is parsed on each
  subparser (correct argparse placement), not the parent.
- `tests/test_cli_json.py`: 16 tests drive `main()` against a live workspace and assert
  both the JSON and plain forms for each command.

- Phase 5 progress: §28–§35, §36, §37, §38, §46, §47, §48, §49, §50 COMPLETE. Full suite: **283 passed**.

#### §46 Onboarding flow — COMPLETE
- `sworker/cli.py`: `cmd_onboard()` — idempotent first-run setup. Runs `init`
  (creates workspace + default `analyst` worker, skips existing files), then
  creates an `admin` user **only if no users exist** (fail-closed: never
  clobbers existing accounts). `--username`/`--password` flags; interactive
  password prompt when `--password` omitted. Prints next-step guidance.
- `onboard` subparser wired in `build_parser`.
- `tests/test_cli_json.py::test_onboard_creates_admin_then_is_idempotent` — first
  run creates the admin user; second run is a no-op ("already exist").

#### §47 Docker + deployment — COMPLETE
- `Dockerfile` (python:3.12-slim, non-root `sworker` user, volume-backed
  `/data`, guided `onboard` then `web --host 0.0.0.0`). Core has zero third-party
  deps, so the image is minimal.
- `.dockerignore`, `docker-compose.yml` (binds 127.0.0.1:8777; front with TLS).
- `web.serve()` + `cmd_web` gain a `--host` flag (default `127.0.0.1`, fail-closed).
  Verified: `web --host 127.0.0.1 --port 8791` serves `/login` (HTTP 200).
- `tests/test_cli_json.py::test_web_help_shows_host_flag`.

#### §48/§49/§50 professional docs — COMPLETE
- `docs/OPERATIONS.md` (§48: install, first run, web UI, Docker, state layout,
  operational checklist).
- `docs/DEMO.md` (§49: 5-minute analyst Q2-revenue walkthrough proving the
  platform guarantees).
- `docs/ARCHITECTURE.md` (§50: runtime model, trust boundaries, verification,
  optional deps, deployment shapes, threat-model summary).
- `tests/test_cli_json.py::test_phase5_docs_present` asserts the three docs +
  Dockerfile + compose ship with the repo.

#### §42 Adversarial test suite — COMPLETE
- `tests/test_adversarial_suite.py` — 31 attack-scenario tests, each a concrete
  adversary move against a shipped fail-closed guard (no mocks, no network):
  - **§12 state machine:** a terminal run cannot be reopened (SUCCESS→EXECUTING,
    CANCELLED→EXECUTING) and terminal states cannot be re-classified
    (BLOCKED→DENIED); illegal transitions raise `IllegalTransition`.
  - **§3 tenant isolation:** a second enforcing store over the *same* state dir
    but a different `workspace_id` is refused `CrossTenantAccess` on the other
    tenant's record; an enforcing store refuses a legacy tenantless record (the
    tenant id is an independent boundary from the filesystem root).
  - **permissions.py AST classifier:** `import socket`, unknown-module import,
    `eval(dynamic)`, and `print(os.system(...))` (dangerous call smuggled inside
    an innocent one) all escalate fail-closed; `bash -c`/`python3 -c` floor at
    EXTERNAL; `rm -rf` is DESTRUCTIVE; an unparseable shell command escalates.
  - **§44 decomposition guard:** once an EXTERNAL action is rejected, a later
    same-or-higher-risk action in the run is refused (cannot be decomposed
    around the rejection).
  - **§54 egress:** empty allow-list denies all; SSRF targets (169.254.169.254,
    metadata.google.internal, 10.x/192.168.x/172.16-31.x) blocked even with a
    permissive allow-list; unlisted host denied; listed host allowed.
  - **§55 DLP:** AWS key + private-key block detected; matched text never
    returned in the refusal; empty policy does not scan; unknown rule fails
    closed.
  - **§4 auth:** wrong password, unknown user, and disabled user all return
    `None` (anti-enumeration); expired and revoked sessions invalid.
  - **§35 redaction:** sensitive keys + emails + long tokens masked by default;
    `redact=False` is required to see plaintext (opt-OUT, never opt-IN).

- Phase 5 progress: §28–§35, §36, §37, §38, §46, §47, §48, §49, §50 COMPLETE;
  §42 adversarial suite ADDED. Full suite: **314 passed**.

#### §44 Prompt-injection defenses — COMPLETE
The platform's primary injection defense is **architectural** (stated in
`engine.py`): the model proposes a plan once from the user request + worker
config; tool/data output flows only into Observations and Artifacts — never
back into the planner, never into the permission decision, never into a new
tool call. §44 adds the **explicit, deterministic** layer on top of that:
  - `sworker/injection.py` (zero-dep): `scan(text)` / `scan_dict(payload)`
    evaluate fixed regex/keyword rules over ingested content and return a
    structured `InjectionVerdict(suspect, rule, kind)`. Fail-closed: unknown/
    ambiguous content is treated as suspect; the matched attacker text is
    NEVER returned (only the fired `rule` name) so it cannot leak into the
    audit log. Seven rule families: instruction-override, system-prompt
    extraction, role-play jailbreak, embedded-command, secret-exfiltration,
    authority-forgery, delimiter-injection.
  - Engine wiring: `WorkerEngine._execute_action` scans every observation's
    `output`/`error`/`data` via `scan_dict` and records the fired rule name on
    `Observation.injection` (new field, defaults `""` = benign/unscanned). The
    flag is auditable but is never fed into `PermissionEngine` and never spawns
    new tool calls — suspect content cannot escalate a run's risk or be treated
    as worker instructions.
  - `tests/test_injection_defense.py`: 14 tests — detector flags each injection
    family, benign business text stays benign, empty/non-string is benign,
    scanned dict finds nested hits, matched text is never echoed, and two
    engine-wired tests prove a poisoned `company/` file is flagged on its
    observation while the read still executes at its normal `read` risk and the
    injected "run the following command" text does NOT spawn a `shell.exec`.
  - Invariant: content the worker ingests is data, not instructions; if it looks
    like an instruction, it is recorded as suspect and ignored as authority.

- Phase 6 progress: §42, §44 COMPLETE. Full suite: **328 passed**.

#### §43 Trust-boundary doc — COMPLETE
`docs/TRUST_BOUNDARY.md` is the per-boundary reference: for each of 14 trust
boundaries it states the *threat*, the *fail-closed guarantee*, the *code
location* (real module + symbol), and the *proving test file* — so the doc
cannot silently drift into fiction. Boundaries covered: model-as-adversary
spine, permission AST classification, decomposition guard, tenant isolation,
egress/SSRF, DLP, connector secret isolation, prompt-injection (§44), auth,
RBAC, web surface, cancellation, hash-chained audit, import/backup safety, and
optional-dependency graceful degradation. Cross-links `docs/ARCHITECTURE.md`
(orientation) and `docs/SECURITY.md` (honest posture).
  - `tests/test_trust_boundary_doc.py`: 6 tests assert the doc exists and names
    only *real* `sworker/*.py` modules, *real* `tests/*.py` files, the core
    invariant, and the cited `WorkerEngine`/`WorkerStore`/`AuthProvider`/
    `ConnectorManager` symbols.
  - `tests/test_cli_json.py::test_phase5_docs_present` extended to require
    `docs/TRUST_BOUNDARY.md`.

- Phase 6 progress: §42, §43, §44 COMPLETE. Full suite: **335 passed**.

#### §45 HITL escalation / quorum — COMPLETE
An approval can now require `quorum` *distinct* human approvers at-or-above a
`min_role`, declared per risk in the worker file:
  `approval_policy: { destructive: {quorum: 2, min_role: operator} }`
Fail-closed rules: a vote below `min_role` is refused (never counted); the same
person voting twice does not advance the quorum (distinct approvers only); a
single REJECT blocks regardless of quorum; settled approvals are immutable;
`quorum` is clamped to ≥1 so it can never require zero approvers. The historical
floor (one human approve settles) is preserved for risks without an
`approval_policy`. Escalation (`ApprovalManager.escalate`) structurally raises
the requirement when a vote cannot be honored.
  - `sworker/approvals.py`: `vote()` (quorum + min_role), `escalate()`,
    `ApprovalError`; `request()` stamps `quorum`/`min_role` from the worker.
  - `sworker/config.py`: `approval_policy` field + `approval_policy_for(risk)`.
  - `sworker/rbac.py`: `role_satisfies()` / `ROLE_LADDER` (escalation ladder).
  - `sworker/models.py`: `Approval.quorum` / `.min_role` / `.votes` / `.escalations`.
  - `cli.py`: `approve`/`deny` gain `--by`/`--role`; quorum-not-met prints a
    clear partial state instead of failing. `web.py`: `/approve`/`/deny` pass the
    logged-in user's role; under-role votes return 403.
  - `tests/test_approval_quorum.py`: 12 tests (default floor, distinct approvers,
    min_role gate, single-reject-blocks, revote, escalation, YAML parsing).

- Phase 6 progress: §42, §43, §44, §45 COMPLETE. Full suite: **346 passed**.

#### §51 Threat model — COMPLETE
`docs/THREAT_MODEL.md` enumerates 10 adversary classes (diverted model, prompt
injection, worker-definition privilege escalation, cross-tenant access, SSRF/
egress, secret exfiltration, auth/session forgery, resource exhaustion, audit
tampering, optional-dependency supply chain), each mapped to the *real* module +
symbol + *real* proving-test file, with an explicit **residual risk** per
boundary and a closing "what this platform does NOT claim" section. A doc-
integrity test (`tests/test_threat_model_doc.py`) fails if any cited
module/symbol/test is removed, so the doc cannot rot into fiction. Cross-linked
from `docs/ARCHITECTURE.md` §6 and added to `test_phase5_docs_present`.
  - `tests/test_threat_model_doc.py`: 5 tests (exists, only-real modules, only-
    real tests, core invariant, cited engine/store/auth/connector/approval/rbac
    symbols).

- Phase 6 progress: §42, §43, §44, §45, §51 COMPLETE. Full suite: **351 passed**.

#### §52 Licensing / packaging — COMPLETE
The package ships as a real, buildable wheel with an honest dependency story:
`pyproject.toml` declares `MIT` license + `LICENSE` file, `version = "0.1.0"`,
`requires-python = ">=3.10"`, and a `sworker = "sworker.cli:main"` console
script. The **core stays dependency-free** (`dependencies = []`); optional
capabilities are opt-in extras — `secrets` (cryptography), `ingest-pdf`
(pdfminer.six), `ingest-docx` (python-docx), `atlas` (hermes-atlas), and `all`.
Verified end-to-end: `pip wheel --no-deps` produces
`sworker-0.1.0-py3-none-any.whl`, it installs, and `sworker --version` prints
`0.1.0`. A static guard (`tests/test_packaging.py`) asserts no third-party
package is imported at module top level (optional libs stay lazy inside the
functions that need them), so a clean install never fails on a missing extra.
  - `tests/test_packaging.py`: 6 tests (LICENSE present + MIT, pyproject license/
    version/name, extras declared + core zero-dep, console-script resolves,
    core imports without third-party, no top-level third-party import in core).

- Phase 6 progress: §42, §43, §44, §45, §51, §52 COMPLETE. Full suite: **357 passed**.

#### §60 Data migration framework — COMPLETE
Stored worker state (sqlite index + audit log + config) evolves across releases;
`sworker/migrations.py` upgrades a workspace from whatever version it was
written with to the current one without re-creating it or losing the audit
trail. `DATA_VERSION` is the logical *data* version (distinct from the sqlite
`schema_version` DDL marker). `MIGRATIONS` maps version `N` → an idempotent
upgrade from `N`→`N+1`; `current_version(store)`, `pending(store)`, and
`migrate(store, to_version=None)` do the rest. Fail-closed: a downgrade target
is refused (no rollback), a target above the highest registered migration is
refused (never guess a future step), and a corrupted marker is refused. Each
applied step is recorded in `meta` (so re-running is a no-op) **and** in the
append-only audit log (`event == "migration"`), so the upgrade itself is
tamper-evident. The CLI `migrate [--to N] [--dry-run] [--json]` exposes it;
`--dry-run` lists pending steps without touching data. First registered step
(v1) stamps the data-version marker and asserts tenant columns are present.
  - `tests/test_migrations.py`: 9 tests (legacy store at v0, apply + audit
    record, idempotent re-run, refuse downgrade, refuse unknown target, no
    pending after migrate, corrupted marker refused, existing data survives,
    CLI dry-run + apply).

- Phase 6 progress: §42, §43, §44, §45, §51, §52, §60 COMPLETE. Full suite: **366 passed**.

#### §61 Graceful degradation — COMPLETE
The platform keeps running when a non-essential capability is unavailable
(no local model -> deterministic fallback plan; Atlas absent -> plaintext grep;
optional crypto absent -> secrets feature disabled). §61 makes every such
degradation **recorded, surfaced, and fail-closed** via `sworker/degradation.py`:
`DegradationLedger.record()` writes each event to the `degradations` store table
(`sworker/store.py`) *and* mirrors it into the append-only audit log
(`event == "degradation.recorded"`), so it is both queryable per-run and
tamper-evident. `Run.degradations` (`sworker/models.py`) surfaces the list on
the run result (printed by the CLI `run` command). Fail-closed: an unknown
severity is treated as `CRITICAL`; a `critical` degradation forces
`WorkerEngine._finalize` to downgrade a would-be `SUCCESS` to
`PARTIAL_SUCCESS` with `run.error` set. Today `model_fallback` (warn, no LLM)
and `knowledge_uncompiled` (warn, Atlas absent but run uses knowledge tools)
are recorded by the engine; `secrets_unavailable` / `sandbox_host` categories
are reserved for the same ledger. Doc: `docs/GRACEFUL_DEGRADATION.md`
(cross-linked from `docs/ARCHITECTURE.md`).
  - `tests/test_degradation.py`: 7 tests (persist + audit, unknown severity ->
    critical, critical downgrades SUCCESS, warn does not, model_fallback
    recorded without LLM, human-readable summary, store round-trip).

- Phase 6 progress: §42, §43, §44, §45, §51, §52, §60, §61 COMPLETE. Full suite: **373 passed**.

#### §62 Safe mode — COMPLETE
A single operator switch that makes a worker **fail closed** instead of acting on
the world during a suspected-bad-state / incident. `sworker/safemode.py`:
`SafeMode` controller over `meta_kv` (`scope == "safemode"`), with levels `off`
/ `readonly` (block everything above `READ` risk) / `locked` (block every tool
action). Fail-closed: an unknown persisted level reads back as `locked`; a
`None`/unknown risk blocks; `locked` blocks all tool actions including
undeterminable risk. The engine (`sworker/engine.py`) reads `SafeMode(store)` once
per `run()` and applies the block at permission-eval time — *before* the normal
approve/deny path — recording a `critical` `safe_mode_block` degradation
(`sworker/degradation.py`) so the run is reported `BLOCKED`, not `SUCCESS`. CLI:
`sworker safemode [status|on|off|readonly|locked]`; web (admin): `GET/POST
/api/v1/safemode`. Doc: `docs/SAFE_MODE.md` (cross-linked from
`docs/ARCHITECTURE.md`).
  - `tests/test_safemode.py`: 11 tests (default off, enable/disable, explicit
    levels, reject unknown, corrupt→locked, readonly blocks >READ, locked blocks
    all, status shape, + 2 engine runs proving readonly/locked → BLOCKED with a
    `safe_mode_block` degradation, and off → no injected degradation).

#### §63 Incident response — COMPLETE
Operator control surface for "the platform is in a bad state." `sworker/incident.py`:
`IncidentLedger` over `meta_kv` (`scope == "incident"`, tenant-scoped) + the
append-only audit log. Declaring an incident engages safe mode `locked` (§62) and
`WorkerEngine.run()` refuses new runs while one is open — returning a `BLOCKED`
`RunResult` with a `critical` `incident_active` degradation (`DegradationLedger`,
`sworker/degradation.py`). Fail-closed: only one incident at a time (a second
`open` is *rejected*, recorded); a corrupt/unrecognised persisted state reads back
as *open + locked*; closing an incident records the closure but does **not**
auto-clear safe mode (operator must explicitly stand down). All transitions are
audit-logged (`incident.opened` / `incident.closed` / `incident.lockdown` /
`incident.rejected`) and replayable via `list_incidents()`. CLI: `sworker incident
[status|open|lockdown|close]`; web (admin): `GET/POST /api/v1/incident`. The
state machine (`sworker/statemachine.py`) was extended so `PLANNING -> BLOCKED` is
legal — an incident freeze blocks a run before any execution. Doc:
`docs/INCIDENT_RESPONSE.md` (cross-linked from `docs/ARCHITECTURE.md`).
  - `tests/test_incident.py`: 9 tests (open engages locked; second open rejected +
    timeline records both; lockdown idempotent; close does not auto-clear safe
    mode; close no-op when inactive; engine run under incident -> BLOCKED with
    `incident_active`; no-incident run not incident-blocked; corrupt state reads
    open fail-closed; status shape).

#### §64 Security events + dashboard — COMPLETE
A curated, queryable **security-event feed** over the append-only, hash-chained
audit log (§13). `sworker/security_events.py`: `SecurityEvents(store)` with a
fixed allow-list catalog (`_EVENT_CATALOG` / `_LABELS`) mapping audited `event`
names to a severity (`info`/`notice`/`warning`/`critical`) and label; `recent()`,
`counts_by_kind()`, `chain_ok()`. Fail-closed: it only ever reports what the
audit log *contains* — never invents events; a degradation record's own severity
is honoured; a `run.transition` is only a security event when it lands on a
fail-closed state (`BLOCKED`/`CANCELLED`/`DENIED`); the `verify_audit_chain()`
verdict is surfaced (a tampered log is *shown*, never hidden). Wired into the
dashboard (`sworker/web.py`): `render_security()` → `GET /security` (HTML) and
`_security_payload()` → `GET /api/v1/security` (JSON: `audit_chain_ok`, counts
by kind, recent events), reusing the same catalog so page/API/CLI can't drift.
CLI: `sworker security [--json] [--kind <kind>] [--limit N]` (added
`cmd_security` + `security` subparser).
  - `tests/test_security_events.py`: 8 tests (incident open/close surfaced as
    critical/notice; safemode change = warning; degradation severity honoured;
    run.transition only when fail-closed; chain verdict reported; kind filter;
    never invents events; both tiers present).

#### §65 "Why blocked?" explainer — COMPLETE
A single fail-closed aggregator over the *already-recorded* block reasons that
were previously scattered across four stores. `sworker/block_explainer.py`:
`BlockExplainer(store)` reads (1) the `degradations` table, (2) `run.error`
tokens (`incident_active` / `resource_exhausted` / unverifiable), (3) per-step
`note` on `BLOCKED` steps (safe-mode block / permission deny / approval
rejection, kinded from the note text), and (4) the incident ledger's platform
freeze; returns `{run_id, status, was_blocked, reasons[], summary}` where each
reason carries `source`/`kind`/`reason`/`severity`/`mitigation`/`detail`.
Fail-closed: it invents **nothing** (every reason traces to real data); unknown
inputs yield `was_blocked = None` (never silently `False`); a `BLOCKED` run with
no discovered reason reports a single `unknown`/`critical` reason (silence is a
finding, not a clean bill); degradation severities pass through. Surfaced three
ways: CLI `sworker why <run_id> [--json]` + `sworker why --workspace [--json]`
(`cmd_why`); web `GET /why?run_id=` (HTML, linked from the run-detail page when
the run is `BLOCKED`) + `GET /api/v1/why[?run_id=]` (JSON) — all three share the
same payload so they can't drift.
  - `tests/test_block_explainer.py`: 9 tests (missing run → None+unknown; incident
    freeze surfaced critical; degradations table + mitigation; run.error tokens;
    step BLOCKED note kinded; BLOCKED-with-no-reason → unknown critical; clean run
    → False; workspace explain; unknown inputs stay None).

#### §66 Composable system-status surface — COMPLETE
A thin, uniform layer over the *already-recorded* hardening signals (§62–§65),
not a re-implementation. `sworker/system_status.py`: `ControlSnapshot` (one shape:
`name`/`severity`/`status`/`source`/`detail`); five adapters (`snapshot_safemode`,
`snapshot_incident`, `snapshot_degradation`, `snapshot_security`,
`snapshot_blocked`) each read only their subsystem's existing public surface (no
subsystem internals modified, so their tests still hold); `SystemStatus(store)
.compose()` runs every adapter and returns `{verdict, generated_at, controls[]}`
with worst-severity-wins (`critical > unknown > warning > ok`). `ADAPTERS` is an
ordered callable list — adding a future control is one appended adapter.
Fail-closed: invents nothing (every snapshot cites its source symbol); a control
that raises is `unknown`, never `ok` (one broken probe can't paint green);
`unknown` outranks `warning`; no noise suppression (every control listed, even
`ok`). Surfaced three ways: CLI `sworker status [--json]`; web (any session)
`GET /status` (HTML, linked from the nav bar) + `GET /api/v1/status` (JSON).
  - `tests/test_system_status.py`: 8 tests (all controls registered; clean
    workspace → ok; incident → critical; safe-mode locked → critical; critical
    degradation → critical; broken probe → unknown not ok; severity ranking; every
    snapshot real-sourced, non-empty status).

#### §68 End-to-end integration tests — COMPLETE
Proves the *whole* platform stack works together through the public
`WorkerEngine` API (no mocks, no cloud, no LLM — deterministic `NullInference`
fallback so it's reproducible anywhere). `tests/test_e2e.py` (8 tests, all read
persisted state, not in-memory objects): a real "total Q2 revenue?" run reaches
`SUCCESS` and states the derived `188,500`, auto-mints `recompute_sum`
verifications that re-sum the same source rows and `PASS`; `verify_audit_chain()`
is `ok` with `checked > 0`; an open `IncidentLedger` incident makes `engine.run()`
return `BLOCKED`/`error=="incident_active"` (fail-closed, not dropped, chain still
intact); `SafeMode(store).lock()` makes a run `BLOCKED` (never `SUCCESS`);
`SystemStatus(store).compose()` over an open incident is `CRITICAL` with the
`incident` control `critical`; `engine.cancel()` moves an `AWAITING_APPROVAL` run
to `CANCELLED` and is idempotent on the terminal run; and the run is fully
reconstructable from `runs`/`steps`/`evidence`/`verifications` (spec principle #4).
Doc `docs/INTEGRATION_TESTS.md`; cross-linked `ARCHITECTURE.md`.

#### §58/§59 Regression & performance benchmarks — COMPLETE
Real measurements of the deterministic engine path (no LLM, no cloud), so they
are reproducible anywhere and catch both correctness *and* perf regressions.
`sworker/benchmark.py`: `run_case(make_engine, request, iterations,
expected_total)` times real wall latency and recovers the `derived_total` from the
run's persisted `recompute_sum` verifications; `run_benchmarks(...)` runs every
case and (with `fail_on_regression=True`) asserts each case's p95 is under its
declared `max_p95_ms` cap. Fail-closed: a case only emits a measurement after a
genuine `SUCCESS` with the expected derived total — a failed run or a wrong/missing
figure raises rather than emitting a placeholder; determinism across iterations is
asserted. `DEFAULT_CASES` carries known derived totals (`188500.0` for total Q2
revenue) and conservative p95 caps. CLI `sworker benchmark [--worker NAME]
[--iterations N] [--no-fail] [--json]` (live: `q2_revenue_total p95≈38ms,
derived=188500.0`). `tests/test_benchmark.py` (7 tests). Doc `docs/BENCHMARKS.md`;
cross-linked `ARCHITECTURE.md`.

#### §70 Maturity model — COMPLETE
A platform's real posture is its **weakest** control, not the average of its
strengths, and never a self-reported badge. `sworker/maturity.py` scores the
running deployment against a five-tier ladder (`none`/`basic`/`standard`/`hardened`/
`sovereign`) by reading only the §42–§68 subsystems' real persisted state — it
invents nothing. The overall level is the **floor** of all dimensions (weakest
link), so a missing auth layer keeps the whole platform low even with a perfect
audit chain. Ten dimensions: audit-chain integrity (`verify_audit_chain`), schema
currency (`migrations.current/pending`), local auth (`AuthProvider.list_users` +
admin/operator), RBAC (privileged role), safe-mode default (`SafeMode.level`),
incident response (opened *and closed*), graceful-degradation awareness
(no critical), security-event visibility (`SecurityEvents.recent`), unified
observability (`SystemStatus.compose` verdict ≠ critical), recovery readiness
(workers + audit ok). Each `Dimension` carries real `evidence` text and, when
below `standard`, a `recommendation`. Surfaces: CLI `sworker maturity [--json]`;
web `GET /maturity` (HTML) + `GET /api/v1/maturity` (JSON, shared payload), nav
linked. `tests/test_maturity.py` (8 tests): fresh deployment resolves low (never
fabricated `sovereign`), auth/incident/safe-mode tiers rise when exercised, floor
== weakest link, `to_dict` shape. Doc `docs/MATURITY.md`; cross-linked `ARCHITECTURE.md`.

- Phase 6 progress: §42, §43, §44, §45, §51, §52, §60, §61, §62, §63, §64, §65, §66, §68, §58, §59, §70 COMPLETE. **Full suite: 443 passed (0 deselected, 0 failures).**

> **Web-test status resolved (post-§70):** the 5 web tests that had been failing
> since the §62 web-auth rewrite were not "drift" — a real bug. The `elif
> url.path == "/run":` branch header was lost during the §62 rewrite, leaving the
> run-handling code as dead/mis-indented inside the `/api/v1/incident` block, so
> every `POST /run` fell through to `404 unknown action`. Restored the `/run`
> branch header in `sworker/web.py`; all 5 web tests now pass and the entire
> suite is green with no deselects.

### Phase 6 — Hardening (continued)
Trust-boundary doc (§43), prompt-injection defenses + tests (§44), HITL
escalation/quorum (§45), threat model (§51), licensing/packaging (§52),
migration infra (§60), graceful degradation (§61), safe mode (§62), incident
response (§63), security events + dashboard (§64), "why blocked?" (§65),
composable interfaces (§66), end-to-end integration test (§68), regression/perf
benchmarks (§58/59), maturity model (§70).

---

## 3. Phase 1 detailed design (immediate build)

### 3.1 Tenant model (§3)
* New `sworker/org.py`: `Organization`, `Workspace` registry (org → workspaces),
  `resolve_workspace(id)` and `list_workspaces(org_id)`.
* `WorkerStore` gains `workspace_id` + `org_id` (constructor param). Every `put` stamps
  them; `get`/`find` accept `workspace=` and **reject** a record whose stored
  `workspace_id` != requested (fail closed — raises `CrossTenantAccess`).
* All `models.Record` subclasses get `org_id`/`workspace_id` fields (default from the
  store at `put` time).
* Cross-workspace test: store A writes a run; store B (different workspace_id) `get` by id
  → raises `CrossTenantAccess`. **Must fail closed.**

### 3.2 Auth (§4) — `sworker/auth.py`
* `User` (username/email, `password_hash` via `hashlib.scrypt`+`secrets.token_bytes`
  salt, no new dependency), `disabled`, `roles`, `created/updated`.
* `Session` (opaque `secrets.token_urlsafe` token, `user_id`, `created`, `expires_at`,
  `revoked`). `create_session`/`validate`/`revoke`/`revoke_all_for_user`.
* `authenticate(username, password)` → session or None (constant-ish time).
* Password change (verify old), disable/enable.
* OAuth/OIDC *shaped*: an `IdentityProvider` protocol so a future provider slots in
  without rewriting authorization (authn ≠ authz separation).

### 3.3 RBAC (§5) — `sworker/rbac.py`
* `ROLES = {owner, administrator, operator, approver, viewer}` with default permission
  sets; `PERMISSIONS` registry of granular strings.
* `Policy(actor) -> has_perm(perm)`; `require(perm)` that raises `AuthorizationError`.
* Enforcement points: `WorkerEngine` (e.g. `runs.execute` gates `run()`), `web.py`
  (every route checks the session's perms), `cli.py` (wraps subcommands).
* Tests: each role's permission boundaries; a viewer cannot `runs.execute`; an approver
  can `approvals.grant` but not `workers.create`.

### 3.4 Policy engine (§6) — extend `sworker/permissions.py`
* New `Policy` dataclass: `version`, `worker`, `fs` (read/write path globs), `network`
  (allow hosts), `tools` (allow list), `risk` (5-key map). Immutable once a run uses it
  (`used_by_run_ids` recorded). `PolicyStore` persists + versions + diffs + exports YAML.
* `PermissionEngine.evaluate` gains: tool-allow check (capability), fs/network grant
  checks, and records the policy `version` on the `Action`. A run records the policy
  version that governed it.
* Backward compatible: existing 5-key `policy:` in worker YAML becomes the `risk` block.

### 3.5 Secrets (§8) — `sworker/secrets.py`
* `Secret` (name, workspace_id, `encrypted_value`, created/updated, rotation metadata,
  access_policy). Encryption: `cryptography`? **No new dep** → use `secrets`+`hashlib`
  is not real encryption. Decision: use Python stdlib **`cryptography` is third-party**,
  so Phase 1 secrets are encrypted with a **key from `SWORKER_MASTER_KEY` (env) via
  `hashlib.scrypt`+`AES` from stdlib `Crypto`?** — stdlib has no AES. OPTIONS:
  (a) depend on `cryptography` (one dep, acceptable for the secrets subsystem, documented
  as the one optional dep), or (b) OS keychain abstraction + envelope noted as
  "stores key outside core". **Plan: add `cryptography` as an OPTIONAL, isolated dep
  behind `secrets.py`** (core runtime still works without it; secrets feature degrades to
  "unavailable" with a clear message). This keeps the zero-dep *runtime* promise for the
  worker execution path while giving real encryption where it matters. Flagged for your
  call in scope confirmation.
* Redaction helper `redact(obj)` used by logging/audit/evidence so secret values never
  persist. The engine refuses to pass secret values into tool args that would log them.

### 3.6 Hash-chain audit (§13) — `sworker/audit.py`
* `Auditor` wraps `store.audit`: computes `event_hash = sha256(prev_hash + canonical(payload))`,
  stores `event_id` + `previous_event_hash` + `event_hash` in the JSONL line.
* `verify_chain(store)` replays and raises on any hash mismatch / missing link.
* CLI `sworker audit verify [workspace]`; web "Security → Audit integrity".
* Backfill: existing lines (no hash) are treated as the trusted genesis; chain starts
  from the first hashed line.

### 3.7 Run state machine (§12) — `sworker/statemachine.py` + `models.py`
* `RunState` enum (the 11 spec states). `TRANSITIONS: dict[RunState, set[RunState]]`
  defining legal moves. `transition(run, new_state, actor, reason)` enforces legality,
  persists a `state_transition` audit event (previous_state, new_state, actor, ts, reason),
  and updates `run.status`. Illegal transition → `IllegalTransition` (fail closed).
* Engine uses `transition()` at every status change (replaces ad-hoc `run.status = ...`).

### 3.8 Cancellation (§11) — `engine.py`
* `WorkerEngine.cancel(run_id, by, reason)`: sets CANCELLED via state machine; if a
  subprocess is live, kill its process group (`os.killpg`); marks current step/action
  CANCELLED; records who/when/current-action/reason. Idempotent.
* Hook cancellation into the subprocess runner (`exec.py`) so a running shell/python
  action is in a tracked process group.

### 3.9 Resource controls (§10) — `WorkerConfig` + `engine.py` + `ToolContext`
* New `WorkerConfig` fields: `max_runtime`, `max_actions`, `max_tool_calls`,
  `max_artifact_size`, `max_python_runtime`, `max_shell_runtime`, `max_network_requests`.
* Engine enforces: total wall-clock `max_runtime` (watchdog thread); step count ≤
  `max_actions`; tool calls ≤ `max_tool_calls`; artifact bytes ≤ `max_artifact_size`;
  network tool calls ≤ `max_network_requests`. Exhaustion → structured failure (run ends
  FAILED/BLOCKED with a `resource_exhausted` reason), never silent.

---

## 3.10 Phase 1 — STATUS: COMPLETE (2026-08-11)

All client-ready security increment sections (§3.1 tenant, §3.6 audit, §3.7
state machine, §3.8 cancel, §3.9 resources, plus §4 auth, §5 rbac, §6 policy,
§8 secrets) are implemented and tested.

* Test count: **60 → 122** (all green on 3.14.6).
* New modules: `org.py`, `auth.py` (§4), `rbac.py` (§5), `policy.py` (§6),
  `statemachine.py`, `secrets.py` (§8).
* New `store.py` tables: `users`, `sessions`, `policies`, `meta_kv`, `secrets`,
  plus `delete()`; `verify_audit_chain()` (§13).
* New `tests/`: `test_org`, `test_auth`, `test_rbac`, `test_policy`, `test_secrets`,
  `test_statemachine`, `test_cancel`, `test_resources`, `test_audit_chain`,
  `test_phase1_integration`.
* CLI hooks: `sworker user {add,disable,enable,list}`, `sworker policy {publish,list,current}`,
  `sworker secret {set,delete,redact,list}` (verified live).
* Secrets (§8) use optional `cryptography` (AES-GCM); core stays zero-dep and
  fails closed (refuses plaintext) if the package is absent.
* Invariants preserved: model-proposes/engine-disposes, real evidence, fail-closed
  permission classification, tenant isolation, append-only auditable truth,
  deterministic no-LLM fallback.

Next: Phase 2 (spec §7 verification hardening, §9 scheduling auth hooks, web UI
auth/RBAC gating) — see spec.

---

## 3.10b Phase 2 — Knowledge — STATUS (2026-08-11)

### §7 Verification hardening — COMPLETE
- `verify.py`: `provenance_chain` check (re-derive source sum AND confirm the
  artifact cites it). `engine._finalize` fails closed: any UNVERIFIABLE
  verification outcome degrades SUCCESS → PARTIAL_SUCCESS.
- Tests: `tests/test_verify_and_procedures.py` (18).

### §9 Scheduling auth hooks — COMPLETE
- `scheduler.py`: `Schedule.created_by`/`last_fired_by`; `add_schedule`/`set_enabled`/
  `remove_schedule`/`mark_fired` stamp actor. `cli.py cmd_sched` enforces
  RBAC `schedule:manage` (fail-closed, exit 3). Verified live.

### Web UI auth/RBAC gating (§11) — COMPLETE
- `web.py` rewritten: real `AuthProvider` session cookie (HttpOnly; SameSite=Strict),
  `_ROUTE_PERMS` map, same-origin CSRF check, static token removed.
- Tests: `tests/test_web_auth.py` (3) + rewritten `tests/test_web.py` (5).

### §15 Generalized verification framework — COMPLETE
- `verify.py`: four new deterministic, model-free checks re-deriving non-numeric
  claims from source:
  - `schema` — required columns + optional `column_types`/`non_null` assertions.
  - `set_equality` — distinct values of a column equal exactly a required set.
  - `regex` — required pattern present/absent in a cited artifact.
  - `doc_section` — exact/substring claim about a headed document section.
- Each follows the existing fail-closed contract (FAIL on mismatch, UNVERIFIABLE
  when source/expected missing). `available_checks()` now lists 11 checks.
- Tests: 13 new (pass/fail/unverifiable per check).
- DEFERRED: `deterministic_script` (run a Python snippet as a check). Arbitrary
  script execution sourced from a YAML procedure is a privilege-escalation/
  prompt-injection surface that contradicts the fail-closed posture; it needs a
  consent-gated sandbox (`SWORKER_ALLOW_EXTERNAL`-style flag + restricted
  interpreter) before it can be added. Noted for a later hardening phase.

### §16 Claim-level provenance + artifact claim exposure — COMPLETE
- `models.Artifact.claim_ids`: claims an artifact surfaces.
- `engine.py`: at artifact-creation time, any claim whose text appears in the
  artifact body is recorded in `claim_ids` (traceability seam).
- `engine._finalize` fails closed: if an artifact surfaces claims but those
  claims have NO provenance (no `evidence_ids`/`verification_ids`), the run is
  degraded SUCCESS → PARTIAL_SUCCESS ("surfaced claim(s) have no provenance").
  A surfaced claim linked to evidence clears the bar → SUCCESS.
- Tests: 2 new (`test_finalize_partial_success_when_artifact_surfaces_unbacked_claim`,
  `test_finalize_success_when_surfaced_claim_is_backed`).

### §17 Atlas deepening — COMPLETE
- `knowledge.py`:
  - `atlas_index_status(atlas_dir)` — fail-closed index report built only on
    Atlas-store primitives (`read_all`/`stats`/`fingerprint`/`changelog`) plus
    each source's stored `checksum`. Detects STALE sources (file edited/deleted
    after compile, via sha256 mismatch) and MISSING sources (file gone). Empty
    `compiled=False` report when Atlas is absent or no index exists — never a
    fabricated status.
  - `incremental_compile(...)` — honest wrapper over `compile_knowledge`
    (`incremental=True`); returns pre/post index status so a caller sees exactly
    what moved. Idempotent when no source changed (changelog silent).
  - `rebuild_index(...)` — full wipe + recompile, confined to the Atlas store
    dir only (never touches the source corpus/workspace); refuses to wipe unless
    the dir looks like an Atlas store (collection dirs present) — fail-closed.
- `cli.py`: `sworker knowledge {status,compile,rebuild}` subcommand.
- Tests: 6 new (`test_knowledge.py`) run against the real `hermes_atlas` checkout
  (skip cleanly if Atlas is unimportable). Cover current-status, stale-edit
  detection, missing-source detection, incremental idempotence, rebuild wipe+recompile,
  and fail-closed no-index.
- Verified live via CLI: init → compile → status → edit (STALE detected) → rebuild.

### §18 ingestion adapters — COMPLETE
- `knowledge.py`:
  - `collect_sources(roots)` — walks knowledge roots, splits into native
    `.md` (Atlas ingest) and adapter sources (`.pdf`/`.docx`/`.json`). Returns a
    fail-closed report: every non-ingestable file (unsupported type, or an
    optional dep that is absent/failed) is recorded in `skipped` with a reason —
    never silently dropped, never fabricated text.
  - `extract_json` — stdlib JSON normalizer to deterministic markdown (objects →
    headed sections; arrays-of-objects → tables; scalars listed). No dependency.
  - `extract_pdf` / `extract_docx` — optional `pdfminer.six` / `python-docx`.
    Return `None` (→ `skipped` reason `pdf-unavailable` / `docx-unavailable`) when
    the library is absent or the file cannot be read. Zero core deps preserved.
  - `compile_knowledge` now ingests adapter sources through Atlas `ingest_file`,
    re-pointing the stored source `path` at the **original** file (so §17 stale /
    missing detection tracks the real document, not a temp copy). Reports
    `markdown_files` / `adapter_files` / `skipped` in the result.
- `cli.py`: `knowledge compile` now prints per-source adapter/skipped breakdown.
- Tests: 4 new (`test_knowledge.py`) — JSON normalization, collect picks up json +
  skips unsupported, full compile ingests json with real path, fail-closed
  no-ingestable report. PDF/DOCX skip paths exercised (deps absent in this env).
- Verified live: init + md + json + pdf → compile ingested 1 md + 1 adapter,
  skipped the pdf as `pdf-unavailable`.

### §19 sync watcher — COMPLETE
- `knowledge.py`:
  - `_roots_snapshot(roots)` — builds a fail-closed `{abspath: (mtime, size)}`
    map of every non-dot file under the knowledge roots (handles single-file
    roots, missing roots, and OSError per-file).
  - `_snapshot_changed(prev, cur)` — True on add/remove/edit (size or mtime).
  - `watch_knowledge(roots, atlas_dir, *, interval=2.0, ...)` — starts a **stdlib
    daemon thread** (no extra deps) that polls every `interval` seconds; on change
    it triggers `incremental_compile` and forwards each result to an optional
    `on_compile` callback. Returns the `threading.Event` used to stop it.
    **Fail-closed:** a compile exception is captured into a
    `{ok:False, reason:"watch-compile-error"}` report (passed to `on_compile`) and
    the loop keeps running — the index is never silently marked current.
- `cli.py`: `knowledge watch [--interval SECONDS]` — blocks, recompiles on
  source changes, logs each recompile (or failure); Ctrl-C stops cleanly.
- Tests: 3 new (`test_knowledge.py`) — watcher recompiles on file edit,
  fail-closed on injected compile error (keeps running + reports), snapshot
  change detection (add/edit/remove).
- Verified live: `knowledge watch` running, editing `company/notes.md` produced a
  recompile with a new fingerprint; `Ctrl-C` stopped it cleanly.

### Phase 2 — COMPLETE
All of Phase 2 (§15 → §19) is built, tested, and verified.

* Test count: **134 → 162** (all green on 3.14.6).
* Zero new third-party deps; model-free verification + fail-closed finalize preserved.

### §20 complete — Phase 3 test-count delta
* Test count: **162 → 200** (all green on 3.14.6). Zero new third-party deps.
  - §20 connectors: +9 (→171)
  - §21 browser hardening: +14 (→185)
  - §22 messaging policy: +8 (→193)
  - §9 execution isolation: +7 (→200)
  - §54 network egress registry: +9 (→209)
  - §55 DLP primitives: +11 (→220)

## 4. Testing strategy (every increment)
* New `tests/test_org.py` (tenant isolation, cross-ws fails closed).
* `tests/test_auth.py` (hash verify, session expiry/revocation, password change, disable).
* `tests/test_rbac.py` (role boundaries, server-side enforcement, negative cases).
* `tests/test_policy.py` (versioning, immutability, capability/fs/network grants, run records version).
* `tests/test_secrets.py` (encrypt/decrypt round-trip, redaction, no secret in audit).
* `tests/test_audit.py` (hash chain builds, tamper detected, `audit verify`).
* `tests/test_statemachine.py` (legal/illegal transitions, transition log).
* `tests/test_cancel.py` (cancel live subprocess, CANCELLED recorded).
* `tests/test_resources.py` (each limit triggers structured failure).
* Full suite stays green (run after every sub-increment).

## 5. Commit cadence
Each numbered item in §3 is its own commit with its tests. No mega-commits. The existing
60-test suite must remain green throughout (backward-compatible changes; deprecate, don't
break, the `sworker run`/`web` flows demonstrated earlier).
