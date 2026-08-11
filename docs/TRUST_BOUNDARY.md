# §43 — Trust Boundary Reference

A precise, per-boundary map of *what the platform refuses to trust* and *how it
fails when trust is broken*. This is the operational companion to
`docs/ARCHITECTURE.md` (orientation) and `docs/SECURITY.md` (honest threat
posture). Every boundary below is **fail-closed**: when input is ambiguous,
unprovable, or out of policy, the safe denial wins — never the grant.

The single spine of the whole system:

> **The model proposes. The engine disposes.** The LLM plans once from the
> user request + the static worker config. Tool/data output flows only into
> Observations and Artifacts — never back into the planner, never into the
> permission decision, never into a new tool call. A retrieved file or web page
> therefore cannot silently steer the run.

---

## 1. The model is an adversary, not an operator

**Threat:** a compromised, diverted, or simply mistaken model is treated as a
first-class adversary. It will try to self-escalate, launder risk through
decomposition, act on secrets it cannot see, or rewrite history.

**Guarantee:** the model holds no authority over system actions. It emits a
plan; `WorkerEngine` is the sole authority that executes, enforces budgets, and
resolves the approval gate (`engine.py`). Policy is static + AST-classified; the
model cannot edit its own worker config.

**Proving tests:** `tests/test_adversarial_suite.py` (§42) — illegal state
transitions, decomposition re-entry, connector default-deny.

---

## 2. Trust boundaries

### 2.1 Permission classification (unknown → highest tier)
**File:** `sworker/permissions.py` — `PermissionEngine` + static AST classifier.
**Rule:** a command is classified by walking its AST. Unknown module imports,
`eval(dynamic)`, smuggled dangerous calls, or an *unparseable* command escalate to
the highest tier the tool can reach (fail-closed). `bash -c` / `python3 -c` floor
at `EXTERNAL`; `rm -rf` is `DESTRUCTIVE`.
**Proving tests:** `tests/test_adversarial_suite.py` — `import socket`, `eval`,
bash/python `-c` floor, `rm -rf`, unparseable shell all escalate.

### 2.2 Decomposition guard (no risk-laundering)
**File:** `sworker/permissions.py` — `DecompositionGuard`.
**Rule:** once an `EXTERNAL` (or pending) action is rejected, a later
same-or-higher-risk action in the same run is refused. You cannot split one
destructive call into ten reversible-looking calls to sneak it through.
**Proving tests:** `tests/test_adversarial_suite.py` — §44 decomposition guard.

### 2.3 Tenant isolation (org-scoped store)
**File:** `sworker/store.py` (`CrossTenantAccess`) + `sworker/org.py`.
**Rule:** every record is stamped with `workspace_id`/`org_id`. `get`/`find` reject
a record whose stored `workspace_id` != requested. Two enforcing stores over the
*same* state dir but different `workspace_id` cannot read each other. Legacy
tenantless records are refused by an enforcing store on reopen (the tenant id
lives in the json blob, not a SQL column — fail-closed).
**Proving tests:** `tests/test_adversarial_suite.py` — cross-tenant + tenantless
record refusal.

### 2.4 Egress (default deny-all + SSRF)
**File:** `sworker/tools/http.py` — `_check_egress`, `SSRF_BLOCKED`, `SSRF_SUBNETS`.
**Rule:** an empty allow-list denies everything. Link-local / cloud-metadata
targets (`169.254.169.254`, `metadata.google.internal`, …) and private subnets
(`10.`, `192.168.`, `172.16–31.`) are blocked *even with* an allow-list entry —
a request to `10.0.0.5` is never "external".
**Proving tests:** `tests/test_adversarial_suite.py` — SSRF targets refused;
`tests/test_egress.py`.

### 2.5 DLP (opt-in, fail-closed)
**File:** `sworker/dlp.py` — `BUILTIN_DLP_RULES`.
**Rule:** payload scanning on egress/messaging only when enabled. A match blocks
the send; an *unparseable* payload blocks (fail-closed), it does not pass.
**Proving tests:** `tests/test_dlp.py`.

### 2.6 Connector secrets (values never reach the model)
**File:** `sworker/connectors.py` — `ConnectorManager`; `engine.py` `_resolve_secret`.
**Rule:** connectors are default-deny (`BUILTIN_CONNECTORS` allow-list). The model
sees only *logical* secret names; the resolver returns the real value
server-side. The secret value is never surfaced into planning or observations.
**Proving tests:** `tests/test_connectors.py`; `tests/test_adversarial_suite.py`.

### 2.7 Prompt-injection (ingested content is data, not instructions)
**File:** `sworker/injection.py` — `scan` / `scan_dict`; `engine.py`
`_execute_action` records `Observation.injection`.
**Rule:** every observation's `output`/`error`/`data` is scanned for
instruction-shaped content (7 rule families). A hit is recorded on the
observation and is *never* fed into the permission decision or used to spawn new
tool calls. The matched attacker text is never echoed into the log.
**Proving tests:** `tests/test_injection_defense.py` (§44) — each family flagged;
a poisoned `company/` file is flagged while the read still executes at `read`
risk and the injected "run the following command" text does not spawn `shell.exec`.

### 2.8 Auth (stdlib, revocable, fail-closed verify)
**File:** `sworker/auth.py` — `AuthProvider`, `authenticate`; `store.py` `users`/`sessions`.
**Rule:** passwords hashed with PBKDF2-HMAC-SHA256 (stdlib `hashlib`). Wrong
password, unknown user, disabled user, expired session, revoked session → `None`.
Constant-ish-time verify; never raises into a grant.
**Proving tests:** `tests/test_auth.py`; `tests/test_adversarial_suite.py` — wrong/
unknown/disabled/expired/revoked → `None`.

### 2.9 RBAC (unknown role / unknown permission → deny)
**File:** `sworker/rbac.py`.
**Rule:** an unknown role is granted nothing; an unknown permission is denied. A
viewer cannot reach `schedule:manage` (the CLI `cmd_sched` exits 3).
**Proving tests:** `tests/test_rbac.py`; `tests/test_scheduler_auth.py`.

### 2.10 Web surface (session + RBAC + CSRF)
**File:** `sworker/web.py`.
**Rule:** no static token. Real `AuthProvider` login → HttpOnly
`sworker_session` cookie. Mutating routes (`/run`, `/approve`, `/deny`,
`/resume`, `/verify`) require a valid session **and** RBAC **and** a CSRF token.
JSON API is versioned under `/api/v1`.
**Proving tests:** `tests/test_web_*.py` — token/CSRF smoke, non-session refusals.

### 2.11 Cancellation (idempotent, kills the group)
**File:** `engine.py` — `cancel`; `tools/base.py` subprocess tracking.
**Rule:** cancel is idempotent on a terminal run (no double-effect). On a live
run it kills the whole process group (`start_new_session=True` + `killpg`
SIGTERM → SIGKILL fallback), so a cancelled run cannot leave a zombie child
spawning further egress.
**Proving tests:** `tests/test_cancel.py` (idempotent on terminal; kills group) +
`tests/test_resources.py` (watchdog).

### 2.12 Audit (append-only, hash-chained)
**File:** `sworker/store.py` — `verify_audit_chain`.
**Rule:** every `put` extends a per-workspace hash chain. Rewriting any past
record breaks verification. Tamper is detectable, not silent.
**Proving tests:** `tests/test_audit_chain.py` (§13) — chain break detected.

### 2.13 Imports / backups (never clobber; secrets excluded)
**File:** `cli.py` — `cmd_import` / `cmd_backup`; `secrets.py`.
**Rule:** import refuses to clobber a non-empty workspace. Backup/export excludes
`secrets.key`. You cannot accidentally overwrite a tenant or leak the key file.
**Proving tests:** `tests/test_package_backup.py` (§31/§32).

### 2.14 Optional dependencies (never required)
**File:** `knowledge.py`, `secrets.py`, `connectors.py`.
**Rule:** `cryptography` (secret at-rest), Hermes Atlas (knowledge),
`pdfminer.six` / `python-docx` (ingestion) are all optional. Absent → graceful
degrade: labeled grep, plaintext + loud warning, or **structured skip with
reason** — never silent-drop, never fabricated text.
**Proving tests:** `tests/test_knowledge.py` (JSON stdlib path; PDF/DOCX skip when
dep absent).

### 2.15 HITL escalation / quorum (§45)
**Files:** `approvals.py` (`ApprovalManager.vote` / `.escalate`), `config.py`
(`approval_policy` → `approval_policy_for`), `rbac.py` (`role_satisfies` /
`ROLE_LADDER`), `models.py` (`Approval.quorum` / `.min_role` / `.votes`).
**Rule:** an approval may require `quorum` *distinct* approvers at-or-above a
`min_role`. A vote below `min_role` is refused (never counted); the same person
voting twice does not advance the quorum; a single REJECT blocks regardless of
quorum; settled approvals are immutable. The historical floor — one human
approve settles — is preserved for risks without an `approval_policy`.
**Proving tests:** `tests/test_approval_quorum.py` (default floor, distinct
approvers, min_role gate, single-reject-blocks, escalation).

---

## 3. The boundary that does NOT exist

There is deliberately **no** boundary that says "trust the model because it is
local." Local models are not safer models; a diverted local LLM is the adversary
§1 describes. Every control above applies identically regardless of which model
backend serves the planner.

---

## 4. How to read a denial

When the engine blocks, the reason is recorded on the run/observation/action and
surfaced via `sworker explain <run_id>` and the web `/dashboard`. A blocked run is
a *correct* run. Silence would be the defect.
