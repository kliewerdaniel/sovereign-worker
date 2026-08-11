# Maturity Model (§70)

The platform's real security/operational posture is whatever its **weakest**
control actually is — not the average of its strengths, and never a self-reported
badge. §70 scores the running deployment against a five-tier ladder by reading
only the existing hardening subsystems' real, persisted state. It invents nothing.

## The ladder

| tier | name | meaning |
|------|------|---------|
| 0 | `none` | control absent / not initialised |
| 1 | `basic` | present but not exercised |
| 2 | `standard` | enforced and recording |
| 3 | `hardened` | enforced, recording, and exercised |
| 4 | `sovereign` | every dimension at standard or above |

## Weakest-link scoring

The overall maturity is the **floor** of all dimensions. A single uninitialised
control keeps the whole platform at `none`/ `basic` rather than letting a strong
audit chain paper over a missing auth layer. This is the fail-closed core of the
model: a flattering aggregate would be a security lie.

## Dimensions (all read real state)

* **Audit-chain integrity** — `store.verify_audit_chain()`; ok ⇒ `standard`, else `none`.
* **Schema currency** — `migrations.current_version` / `pending()`; no pending ⇒ `standard`.
* **Local authentication** — `AuthProvider.list_users()`; users + admin/operator ⇒ `standard`.
* **Role-based access** — roles assigned + a privileged role ⇒ `standard` (§45).
* **Safe-mode default** — `SafeMode.level()`; operator-chosen non-off ⇒ `hardened`, default `off` ⇒ `standard` (§62).
* **Incident response** — `IncidentLedger` opened *and closed* (exercised) ⇒ `hardened` (§63).
* **Graceful-degradation awareness** — `DegradationLedger` with no critical ⇒ `standard` (§61).
* **Security-event visibility** — `SecurityEvents.recent()` recording ⇒ `standard` (§64).
* **Unified observability** — `SystemStatus.compose()` verdict not `critical` ⇒ `standard` (§66).
* **Recovery readiness** — workers defined + audit chain ok ⇒ `standard` (§3/§13).

Each dimension carries an `evidence` string (what was actually queried) and, when
below `standard`, a `recommendation` naming the next step.

## Surfaces

* CLI: `sworker maturity [--json]` — prints `LEVEL`, one row per dimension with its
  tier, and a `summary` line ("N/M dimensions at standard+").
* Web: `GET /maturity` (HTML table) + `GET /api/v1/maturity` (JSON, shared payload).
  Nav-bar linked.
* `sworker/maturity.py`: `MaturityModel(store, label).assess() -> MaturityReport`
  with `.to_dict()`; convenience `assess_maturity(store, label) -> dict`.
* `tests/test_maturity.py` (8 tests): fresh deployment resolves low (never
  fabricated `sovereign`); auth/incident/safe-mode tiers rise correctly when
  exercised; floor == weakest link; `to_dict` shape + the helper.
