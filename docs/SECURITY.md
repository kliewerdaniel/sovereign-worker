# Security model — honest notes

`sovereign-worker` is a **local-first AI worker runtime**. The whole safety
story rests on two facts: the model *proposes* and the engine *disposes*, and
nothing it claims is taken on faith — every figure is re-derived from source.
This document states what is actually enforced and, just as importantly, what is
**not** — so the limits are not hidden in docstrings.

## 1. The permission model

- **The model proposes; the engine disposes.** Every tool call is routed through
  `PermissionEngine.evaluate` *before* it runs. The model never sees or alters
  that decision, and a tool can never widen its own filesystem boundary
  (`ToolContext.resolve` realpaths both sides and rejects escapes, including the
  `/tmp` → `/private/tmp` symlink on macOS).
- **The tool's declared risk is a floor.** Context (the concrete call) can only
  ever *raise* the effective risk, never lower it below `tool.risk`.
- **Decomposition does not launder risk.** `DecompositionGuard` records a risk
  ceiling a human has rejected or left pending in a run, and blocks any
  equal-or-higher-risk action from sneaking in afterwards.

### Risk classification: static and fail-closed

`classify()` does **not** string-match keywords (that is trivially bypassed with
`subprocess.run(...)`, `os.system(...)`, `__import__('socket')`, etc.).

- **`python.run`** is classified by parsing the submitted code with `ast` and
  walking the real import/call graph:
  - network / IPC / process-bridging modules (`subprocess`, `os.system`,
    `os.popen`, `socket`, `urllib`, `requests`, `httpx`, `ftplib`, `smtplib`,
    `ctypes`, …) → **EXTERNAL**;
  - filesystem-destructive calls (`shutil.rmtree`, `os.remove`, `os.unlink`,
    `pathlib.Path.unlink`, …) → **DESTRUCTIVE**.
  - **Fail closed:** any `ast.parse` failure, any dynamic `eval` / `exec` /
    `compile` / `__import__` / `getattr` with a non-literal argument, or any
    import/call the walker does not positively recognise is escalated to the
    **highest tier the tool can reach**. It never silently defaults to the safe
    floor.
- **`shell.exec`** resolves `argv[0]` (already allowlist-checked via
  `shell_allow`) and additionally floors **interpreter binaries** —
  `python3 -c "…"`, `bash -c "…"`, `perl`, `node`, `ruby`, `osascript`, … — at
  **EXTERNAL** regardless of the rest of the argv, because their behaviour cannot
  be verified from the command line the way a fixed-purpose binary like `ls` or
  `cat` can.
- **`http.get`** is **READ** only for localhost targets; any remote host is
  **EXTERNAL**. `http.post` is always **EXTERNAL** (it sends data off the
  machine).

The curated module lists are deliberately conservative, not exhaustive. The
intent is that a security-literate reader cannot construct a string that evades
the floor — see `tests/test_permissions.py`.

## 2. Execution sandbox (shell + python)

These run as **subprocesses on the host, not in a VM**. The boundaries enforced
are real but **shallow**:

- `argv[0]` allowlist (`shell_allow`);
- cwd pinned to the workspace;
- env allowlist (`env_allow`) — secrets do not leak by default;
- wall-clock timeout;
- output cap;
- no shell metacharacter interpretation (`shell=False`, parsed with `shlex`).

A determined process can still reach the network and read anything the invoking
user can read. **Set `sandbox: docker` (or run the worker under a real sandbox)
on the worker for genuine isolation** — the built-in controls are a policy floor,
not a security boundary against a hostile worker definition.

## 3. HTTP egress (SSRF surface)

`tools/http.py` already treats network egress as a boundary crossing
(**EXTERNAL** risk for anything beyond a plain GET to an allowlisted host).
Caveats that remain the operator's responsibility:

- `auth_env` reads a credential from the process environment, but **only** if the
  variable name is in the worker's `env_allow` list — an undeclared credential is
  refused.
- There is no SSRF allowlist on *target* hosts beyond the localhost READ
  downgrade. A worker granted `http.post` to arbitrary remote URLs can exfiltrate
  data; restrict which workers have `http.*` in their `tools:` list.
- Only `http`/`https` schemes are accepted; everything else is rejected.

## 4. Git tools

Read operations are free, commits are reversible, **`git push` is EXTERNAL**
(it ships local state to a remote). Confine workers to read/diff/commit unless a
push is genuinely part of their job.

## 5. Web UI (CSRF / auth)

The web UI binds to `127.0.0.1` only — that is **not** a CSRF defense. Any page
open in the same browser could otherwise POST to `/approve` or `/resume` with no
token. Therefore:

- every state-changing request (`/run`, `/approve`, `/deny`, `/resume`,
  `/verify`) requires a **per-session token** (printed to stdout at startup,
  supplied as `?token=` or `X-SW-Token`);
- the request's `Origin`/`Referer` must be same-origin (empty, or
  `http://127.0.0.1:<port>`);
- a request failing either check gets `403`.

Read-only GETs (`/`, `/run?...`, `/verify?...`, `/api/runs`) need no token. Pass
`--token <fixed>` if you front the server with a reverse proxy that already
enforces authentication.

## 6. Storage

Everything lives under the workspace root. `WorkerStore` keeps an sqlite *index*
for fast queries and an append-only JSONL *audit log* as the truth — one line per
event, never edited, never deleted, reconstructable even if the database is
dropped. See `store.py` for the SQL allowlisting that keeps query parameters
off the table/column/order clauses. **Nothing leaves the machine** unless a tool
explicitly performs egress (HTTP, `git push`, or an EXTERNAL subprocess).

## 7. What this is NOT

- Not a hardened multi-tenant sandbox. One `sworker` process runs with the
  invoking user's privileges.
- Not a substitute for reviewing worker YAML. The worker definition is the
  security policy — treat it like code (it lives in version control).
- Not a guarantee against a *compromised dependency*. The core has zero
  third-party dependencies precisely to keep that surface small.
- Not domain-specific. The runtime is domain-independent by construction: a
  static guard in `tests/test_runtime_worker_contract.py` fails the build if any
  `if worker.name == "sales":` branch appears in `engine.py`. Domains are added
  as data (`WorkerConfig`) + opt-in `Tool` subclasses, never engine forks. The
  Sales Worker (`sworker/sales/`) is the first reference implementation; see
  `docs/BUILDING_A_WORKER.md` to define the next one without touching the core.
