# §23 — Procedure Registry

A **procedure** is a named, versioned, reviewable recipe a worker can run.
Procedures are YAML files: diffable, frozen on publish, and content-hashed so a
published release is auditable and reproducible. The registry is the
human-reviewable ledger of *released* procedures — distinct from draft
procedures living in a worker's `procedures/` dir.

## Why it exists

Untracked, ad-hoc prompts drift. A procedure that produced a good outcome
should become a frozen, named artifact a human reviewed and pinned — not a
copy-paste string that quietly changes next week. Publish = a deliberate,
RBAC-gated, reviewed release. Rollback = re-point the active pin to a prior
version. Nothing is ever silently overwritten.

## Implementation (`sworker/procedures.py`)

- `procedures_dir(worker)` / `published_dir(worker)` — draft vs. released layout.
  Published releases live under `<procedures>/published/<name>/<version>.yaml`
  with a `current.txt` pin recording the active version.
- `next_procedure_version(worker, name)` — deterministic `major.minor`
  increment; first publish is `1.0`.
- `publish_procedure(worker, name, body, author, force=False)` — **fail-closed**:
  refuses to overwrite an existing version unless `force` (never clobbers a
  reviewed release); prepends `# published_by` / `# published_at` / `# version`
  meta lines; writes a `current.txt` pin; returns `{name, version, hash}`.
- `rollback_procedure(worker, name, version="")` — re-points `current.txt`. With
  no version, rolls back to the previous sorted version; refuses if already at
  the earliest or the named version does not exist.
- `list_published(worker)` — every published version with author + SHA-256 hash.
- `current_version(worker, name)` / `procedure_published(worker, name)` — the
  active pin and its loaded body.
- `can_publish(rbac, role)` — RBAC gate requiring `procedure:publish`
  (see `sworker/rbac.py`; only `operator`/`admin` hold it).

## Surfaces

- **CLI** (`sworker/cli.py`, `cmd_procedure`): `procedure list <worker>`,
  `procedure publish <worker> <name> [--body B | --file F] --author A --role R`,
  `procedure rollback <worker> <name> [--version V] --role R`. All publish/rollback
  paths are RBAC-gated via `can_publish`; `--json` available.
- **Web review ledger** (`sworker/web.py`): `GET /procedures` renders a per-worker
  table of published versions (name / version / current pin / author / hash);
  `GET /api/v1/procedures` returns the same as JSON `{"procedures": [...]}`. The
  ledger shows **only metadata** — procedure bodies are never inlined on the
  page or the API (the editor is the CLI; the web surface is the review view).
  Nav bar linked from the security/status rows.

## Tests

- `tests/test_procedures_publish.py` — publish, version bump, force-overwrite
  refusal, rollback to previous / named / earliest-refusal, fail-closed paths.
- `tests/test_verify_and_procedures.py` — procedure↔verification wiring.
- `tests/test_web_procedures.py` — live HTTP: `/procedures` lists published
  metadata without leaking bodies; `/api/v1/procedures` returns the JSON mirror.

## Discipline notes

- **Fail-closed**: publish refuses to overwrite; rollback refuses unknown/earliest;
  no `procedure:publish` role → hard deny (never anonymous-authorized release).
- **Reproducible**: every published file carries a SHA-256 in `list_published`,
  so a review comparison is a hash diff, not a trust statement.
- **Web is review-only**: the registry's *mutation* stays on the CLI (where the
  RBAC actor and author are explicit); the web UI surfaces the frozen ledger.
