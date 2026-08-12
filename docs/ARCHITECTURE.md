# §50 — Architecture & Security Model

A concise reference for operators and reviewers. Full spec per-section lives in
`docs/ROADMAP.md`; this is the 10-minute orientation.

---

## 1. Runtime model

```
REQUEST ─▶ INTENT ─▶ PLAN ─▶ ACTION ─▶ TOOL ─▶ OBSERVATION ─▶ EVIDENCE
   │                                                          │
   └──────────────── VERIFICATION ◀ ARTIFACT ◀ APPROVAL ◀────┘
                              │
                           FINAL ─▶ AUDIT (append-only, hash-chained)
```

- **Worker**: a YAML identity (`name`, `role`, `instructions`, `tools`,
  `policy`, optional `connectors`/`triggers`). The engine loads it; the model
  never edits it.
- **Engine**: the single authority. It plans, executes, enforces budgets
  (max actions / tool calls / runtime), resolves the approval gate, and
  finalises. **The model proposes; the engine disposes.** The LLM is never an
  authority over system actions.
- **Store**: a local SQLite ledger with a hash chain (`verify_audit_chain()`)
  so tampering with any past record breaks the chain.

> **Runtime vs Worker (the core abstraction).** The platform is *one runtime* and
> *many workers*. The runtime owns planning, execution, permissions, tool dispatch,
> observations, evidence, verification, artifacts, approval, persistence, replay, and
> audit — **none of it branches on worker identity**. A worker is a pure data object
> (`WorkerConfig`): an identity plus a policy plus a tool allow-list plus a knowledge
> scope plus procedures plus a domain config. To add the Sales Worker we defined a new
> `WorkerConfig` instance and a boundary-layer package (`sworker/sales/`); **zero lines
> of the engine were changed for the domain**. A static guard in
> `tests/test_runtime_worker_contract.py` fails the build if anyone sneaks a
> `if worker.name == "sales":` branch into `engine.py`. The Sales Worker is the
> *first reference implementation* of this boundary — see `docs/BUILDING_A_WORKER.md`
> to define the next one (a Research Worker, a Finance Worker, …) without touching
> the core.

---

## 2. Trust boundaries (fail-closed everywhere)

| boundary | rule |
|----------|------|
| Permission classification | static AST walk; unknown → **highest tier the tool can reach** |
| Decomposition guard | a rejected/pending risk ceiling blocks equal-or-higher re-entry |
| Tenant isolation | org-scoped store access; cross-tenant reads refused |
| Egress | default **deny-all**; allow-list + SSRF checks |
| DLP | opt-in payload scanning on egress/messaging; fail-closed |
| Connector secrets | only logical names reach the model; values resolved server-side |
| Auth | PBKDF2-HMAC-SHA256 (stdlib); sessions revocable; fail-closed verify |
| RBAC | unknown role / unknown permission → deny |
| Web | per-session cookie + RBAC + CSRF; JSON API versioned under `/api/v1` |
| Cancellation | idempotent on terminal; kills the process group |
| Imports / backups | refuse to clobber non-empty workspace; `secrets.key` excluded |

---

## 3. Verification guarantees

Every figure a run emits is independently re-derived from source data via
declared checks (`schema`, `set_equality`, `regex`, `doc_section`,
`provenance_chain`, …). A run that surfaces a claim with no backing evidence,
or skips a required check, finalises as **`PARTIAL_SUCCESS`** — it can never
silently report `SUCCESS` with an unbacked claim.

---

## 4. Optional dependencies (never required)

- `cryptography` — secret encryption at rest (`secrets.py`). Absent → plaintext
  + a loud warning. Core runtime stays zero-dep.
- `Hermes Atlas` — compiled knowledge retrieval. Absent → labelled grep.
- `pdfminer.six` / `python-docx` — PDF/DOCX ingestion. Absent → structured
  **skip with reason**, never silent-drop, never fabricated text.

---

## 5. Deployment shapes

- **Local**: `sworker run <worker> "…"` from a terminal.
- **Service**: `sworker web` (front with TLS on a trusted network).
- **Container**: `docker compose up --build` (non-root, volume-backed state).

See `docs/OPERATIONS.md` and `docs/DEMO.md`.

---

## 6. Threat model (summary)

The platform assumes a **compromised or misguided model** is a first-class
adversary. Defenses are structural, not prompt-level:

- The model cannot grant itself capabilities (no tool-self-modification;
  policy is static + AST-classified).
- The model cannot launder risk through decomposition (DecompositionGuard).
- The model cannot act on secrets it cannot see (connector resolver).
- The model cannot rewrite history (hash-chained audit).
- A human sits on the external/financial/destructive gate by default.

A full adversarial test suite, a per-boundary trust reference, and a detailed
threat model are tracked in `docs/ROADMAP.md` (§42, §43, §51) and
`docs/TRUST_BOUNDARY.md`. The detailed adversary enumeration lives in
`docs/THREAT_MODEL.md`. How the platform degrades safely when a non-essential
capability is unavailable — without ever silently claiming a clean success — is
in `docs/GRACEFUL_DEGRADATION.md` (§61). How an operator freezes a worker so it
fails closed during an incident — without ever silently disabling the guard — is
in `docs/SAFE_MODE.md` (§62). How an operator declares a live incident, freezes
the platform (`locked`), refuses new runs, and keeps a tamper-evident timeline —
without ever silently standing the platform back down — is in
`docs/INCIDENT_RESPONSE.md` (§63). The curated, queryable **security-event feed**
over the tamper-evident audit log, and where it surfaces in the dashboard — is in
`docs/SECURITY_EVENTS.md` (§64). How an operator gets one fail-closed answer to
"why was this run (or the platform) blocked?" when the reasons are scattered
across incidents, degradations, per-step notes, and `run.error` — is in
`docs/WHY_BLOCKED.md` (§65). The composable, worst-severity-wins **system-status
surface** that aggregates all of those independent controls (safe mode, incident
freeze, degradations, security-event feed + audit-chain verdict, block reasons)
into one fail-closed verdict — without re-implementing any of them — is in
`docs/SYSTEM_STATUS.md` (§66). The **end-to-end integration tests** that drive the
real `WorkerEngine` (deterministic, no LLM) through a full run, the audit-chain
verification, the §62/§63 freeze gates, the §66 aggregation, and §11
cancellation — and assert only on persisted state — are in `docs/INTEGRATION_TESTS.md` (§68). The **regression & performance benchmarks** that time the real deterministic engine path and assert p95 thresholds (catching both correctness and latency regressions) are in `docs/BENCHMARKS.md` (§58/§59). The **maturity model** — weakest-link scoring of the deployment's real hardening posture across ten dimensions — is in `docs/MATURITY.md` (§70). The **procedure registry** — named, content-hashed, semver-ish versioned releases with fail-closed publish/rollback and a `procedure:publish` RBAC gate, surfaced as a review ledger over the web UI (`/procedures`, `/api/v1/procedures`) and the CLI — is in `docs/PROCEDURES.md` (§23).
