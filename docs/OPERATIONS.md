# §48 — Deployment & Operations Guide

How to run the Sovereign AI Worker Platform in production-like settings. The
platform is **local-first**: no cloud, no model API required to run. State
lives under `SWORKER_HOME` (default `.sworker` in the current directory, or a
path you set).

---

## 1. Install

Core has **zero third-party dependencies** (Python 3.10+). From a checkout:

```bash
cd sovereign-worker
python -m pip install .
# entry point registered: sworker
sworker --help
```

Optional: for *compiled* company-knowledge retrieval, install
[Hermes Atlas](https://github.com/NousResearch/hermes-atlas) alongside. Without
it, knowledge search degrades to labelled grep — every capability still works.

---

## 2. First run (guided)

```bash
export SWORKER_HOME=/var/lib/sworker
sworker onboard --username admin --password "<strong>"
```

`onboard` is **fail-closed**: it creates the workspace + a default `analyst`
worker, and creates an admin user **only if no users exist** (it never clobbers
existing accounts). Run it again safely — it is idempotent.

---

## 3. Web UI

```bash
sworker web --host 127.0.0.1 --port 8777
```

- Binds **127.0.0.1 by default** (fail-closed). Only expose it behind a TLS
  reverse proxy on a trusted network — the server itself serves plain HTTP and
  is not a network boundary.
- Auth is per-session (HttpOnly cookie) + RBAC + CSRF. There is no static
  startup token; create accounts with `sworker user add`.
- Admin dashboard: `/dashboard` (aggregates workspace health, run counts,
  metrics). Versioned JSON API under `/api/v1/...` (OpenAPI at
  `/api/v1/openapi.json`).

---

## 4. Docker

```bash
docker compose up --build
# mounts a named volume at /data, exposed on 127.0.0.1:8777
```

The image is minimal (python:3.12-slim, non-root `sworker` user). Override
admin credentials with `SWORKER_ADMIN` / `SWORKER_ADMIN_PASSWORD`. Secrets are
encrypted at rest in `secrets.key` (optional `cryptography` dep; degrades to
plaintext + a loud warning if absent). **Never** include `secrets.key` in an
export/backup — the tool deliberately omits it.

---

## 5. State layout (`$SWORKER_HOME`)

| path | purpose |
|------|---------|
| `workers/` | worker YAML identities |
| `state/` | runs, audit ledger, users, sessions, policies, secrets, knowledge index |
| `company/` | business data for the default analyst worker |
| `artifacts/` | run outputs |

Back up with `sworker backup create <dest>`; restore with `sworker backup
restore <path>` (refuses to clobber a non-empty workspace). Package workers +
state for transfer with `sworker package export <dest>` (excludes `secrets.key`).

---

## 6. Operational checklist

- [ ] `sworker doctor` reports no `error`-severity check (audit-chain integrity,
      worker config parse, secrets-key presence).
- [ ] All workers have explicit `policy` blocks (read/reversible/external/
      financial/destructive) — unknown classifications fail closed to the highest
      tier the tool can reach.
- [ ] Egress allow-list (`sworker egress policy`) set; default is **deny-all**.
- [ ] DLP rules (`sworker dlp policy`) configured where payloads leave the box.
- [ ] Secrets stored via `sworker secret set`, never in worker YAML.
- [ ] Web UI fronted by TLS; admin password changed from the bootstrap default.
