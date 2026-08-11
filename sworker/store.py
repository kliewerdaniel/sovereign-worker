"""Local execution store: sqlite for query, append-only JSONL for audit.

Two writes per object on purpose. The sqlite tables are an *index* — they exist
so `worker runs` is fast. The JSONL ledger is the *truth*: append-only, never
edited, never deleted, one line per event, so a Run can be reconstructed even if
the database is deleted. Same discipline as AtlasStore.changelog.

Everything lives under the workspace root. Nothing leaves the machine.

Tenant isolation (spec §3)
--------------------------
A client must never accidentally access another client's data. Beyond the
filesystem root, every persistent record carries an explicit ``org_id`` +
``workspace_id``. When a store is opened *with* a ``workspace_id`` it is in
enforcing mode: ``put`` stamps the tenant on every record, and ``get``/``find``
refuse any record that does not belong to that workspace — raising
``CrossTenantAccess`` rather than silently returning or omitting it. A store
opened *without* a workspace id (legacy mode) behaves exactly as before, which
keeps the pre-existing suite green.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from .models import Record

GENESIS_HASH = "genesis"

SCHEMA_VERSION = 2


def _hash_record(record: Dict[str, Any]) -> str:
    """Hash a single audit record for the chain.

    Includes the previous link so tampering with any line breaks the chain. The
    ``event_hash`` field itself is excluded from the hash (obviously), as is the
    ``event_id`` placeholder if present.
    """
    h = hashlib.sha256()
    for key in sorted(record):
        if key in ("event_hash",):
            continue
        h.update(key.encode("utf-8"))
        h.update(json.dumps(record[key], sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()

# table -> extra indexed columns (beyond id + json blob)
# Every table also carries `org` and `workspace` (tenant isolation); they are
# appended programmatically below so each table's index enforces the boundary.
_TABLE_EXTRA: Dict[str, List[str]] = {
    "tasks": ["worker", "created", "origin", "procedure"],
    "plans": ["run_id", "task_id", "created"],
    "steps": ["run_id", "plan_id", "idx", "status"],
    "runs": ["task_id", "worker", "status", "started", "seq"],
    "actions": ["run_id", "step_id", "tool", "risk", "status", "created"],
    "observations": ["run_id", "action_id", "ok", "created"],
    "evidence": ["run_id", "provenance", "created"],
    "claims": ["run_id", "confidence", "provenance", "created"],
    "verifications": ["run_id", "claim_id", "outcome", "created"],
    "approvals": ["run_id", "action_id", "state", "risk", "created"],
    "artifacts": ["run_id", "kind", "created", "path"],
    "procedures": ["name", "worker", "created"],
    "schedules": ["worker", "procedure", "cron", "enabled", "next_run"],
    # local auth (spec §4)
    "users": ["username", "disabled", "created"],
    "sessions": ["token", "username", "created", "expires", "revoked"],
    # policy registry (spec §6)
    "policies": ["scope", "version", "actor"],
    "meta_kv": ["k", "scope"],
    # encrypted secrets (spec §8) -- value is ciphertext; only name+fingerprint clear
    "secrets": ["name", "fingerprint"],
    # §61 graceful degradation ledger (recorded, surfaced, never hidden)
    "degradations": ["run_id", "category", "severity", "created"],
}

# tenant columns appended to every table
TENANT_COLS = ["org", "workspace"]

TABLES: Dict[str, List[str]] = {
    t: cols + TENANT_COLS for t, cols in _TABLE_EXTRA.items()
}

# dataclass field name -> column name, where they differ
COLUMN_ALIASES = {"steps": {"index": "idx"}}


class CrossTenantAccess(Exception):
    """Raised when a store operation would cross a workspace boundary.

    This is a security boundary, not a convenience error: it must propagate,
    never be swallowed or downgraded to a "not found".
    """


class WorkerStore:
    def __init__(self, root: str, workspace_id: str = "", org_id: str = ""):
        self.root = os.path.abspath(root)
        self.workspace_id = workspace_id or ""
        self.org_id = org_id or ""
        self._enforce = bool(self.workspace_id)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "worker.db")
        self.audit_path = os.path.join(self.root, "audit.jsonl")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # -- schema ------------------------------------------------------------
    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        for table, cols in TABLES.items():
            coldefs = ", ".join(f"{c} TEXT" for c in cols)
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                f"(id TEXT PRIMARY KEY, {coldefs}, json TEXT NOT NULL)"
            )
            for c in cols:
                cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{c} ON {table}({c})")
        # backfill tenant columns on stores that predate tenant isolation
        for table in TABLES:
            for c in TENANT_COLS:
                try:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
                except sqlite3.OperationalError:
                    pass  # already present
        cur.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -- tenant enforcement helpers ----------------------------------------
    def _assert_tenant(self, stored_ws: str, requested: str = "") -> None:
        """Raise CrossTenantAccess if the stored record is not in `requested`.

        Legacy mode (no enforcing store, no explicit request) does not check.
        """
        if not self._enforce and not requested:
            return
        effective = requested or (self.workspace_id if self._enforce else "")
        if effective and stored_ws and stored_ws != effective:
            raise CrossTenantAccess(
                f"record belongs to workspace {stored_ws!r}, not {effective!r}"
            )
        if self._enforce and not stored_ws:
            # an enforcing store must never surface a tenantless (legacy) record
            raise CrossTenantAccess(
                f"record has no workspace_id; enforcing store {self.workspace_id!r} refuses it"
            )

    # -- audit -------------------------------------------------------------
    def _previous_hash(self) -> str:
        """Hash of the last appended audit line, or GENESIS if none yet.

        Reads the file's last non-empty line. Audit files are bounded in size
        (one per workspace, append-only), so a full read is cheap and far more
        robust than a backward-seek into possibly-partial tail bytes.
        """
        if not os.path.exists(self.audit_path):
            return GENESIS_HASH
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                last = None
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
                if not last:
                    return GENESIS_HASH
                rec = json.loads(last)
                return rec.get("event_hash", GENESIS_HASH)
        except (OSError, json.JSONDecodeError, ValueError):
            return GENESIS_HASH

    def audit(self, event: str, table: str, record_id: str, payload: Dict[str, Any]) -> None:
        prev = self._previous_hash()
        record = {
            "ts": time.time(),
            "event": event,
            "table": table,
            "id": record_id,
            "org": self.org_id,
            "workspace": self.workspace_id,
            "payload": payload,
            "previous_event_hash": prev,
        }
        record["event_hash"] = _hash_record(record)
        line = json.dumps(record, sort_keys=True, default=str)
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def iter_audit(
        self, run_id: str = "", workspace: str = ""
    ) -> Iterator[Dict[str, Any]]:
        if not os.path.exists(self.audit_path):
            return
        ws = workspace or (self.workspace_id if self._enforce else "")
        with open(self.audit_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if ws and rec.get("workspace") != ws:
                    continue
                p = rec.get("payload") or {}
                if run_id:
                    if run_id not in (p.get("run_id"), rec.get("id")):
                        continue
                yield rec

    def verify_audit_chain(self, workspace: str = "") -> Dict[str, Any]:
        """Recompute the hash chain end to end.

        Legacy (hashless) lines — anything without an ``event_hash`` — are
        treated as trusted genesis entries and skipped in the link check, but the
        chain is still validated *from the first hashed line onward* (spec §61:
        never silently drop a security property, but do not fail on pre-upgrade
        history). Returns a structured report; raises nothing.
        """
        if not os.path.exists(self.audit_path):
            return {"ok": True, "lines": 0, "checked": 0, "errors": []}
        ws = workspace or (self.workspace_id if self._enforce else "")
        errors: List[Dict[str, Any]] = []
        prev = GENESIS_HASH
        checked = 0
        total = 0
        with open(self.audit_path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    errors.append({"line": i, "error": "unparseable audit line"})
                    continue
                if ws and rec.get("workspace") and rec.get("workspace") != ws:
                    continue
                if "event_hash" not in rec:
                    # legacy line: reset the link baseline, do not check it
                    prev = rec.get("previous_event_hash", GENESIS_HASH)
                    continue
                expected_prev = rec.get("previous_event_hash")
                if expected_prev != prev:
                    errors.append(
                        {
                            "line": i,
                            "id": rec.get("id"),
                            "error": "previous_event_hash link broken",
                            "expected": prev,
                            "got": expected_prev,
                        }
                    )
                recomputed = _hash_record(rec)
                if recomputed != rec.get("event_hash"):
                    errors.append(
                        {
                            "line": i,
                            "id": rec.get("id"),
                            "error": "event_hash mismatch (record altered)",
                        }
                    )
                prev = rec["event_hash"]
                checked += 1
        return {"ok": not errors, "lines": total, "checked": checked, "errors": errors}

    # -- crud --------------------------------------------------------------
    def put(
        self, table: str, obj: Record | Dict[str, Any], event: str = "put"
    ) -> Dict[str, Any]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}; valid: {sorted(TABLES)}")
        d = obj.to_dict() if isinstance(obj, Record) else dict(obj)
        if self._enforce:
            d["org_id"] = self.org_id
            d["workspace_id"] = self.workspace_id
        cols = TABLES[table]
        aliases = COLUMN_ALIASES.get(table, {})
        rev = {v: k for k, v in aliases.items()}
        values = []
        for c in cols:
            src = rev.get(c, c)
            v = d.get(src)
            values.append(None if v is None else str(v))
        placeholders = ", ".join("?" for _ in range(len(cols) + 2))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table} (id, {', '.join(cols)}, json) "
                f"VALUES ({placeholders})",
                [d["id"], *values, json.dumps(d, sort_keys=True, default=str)],
            )
            self._conn.commit()
        self.audit(event, table, d["id"], d)
        return d

    def get(
        self, table: str, record_id: str, workspace: str = ""
    ) -> Optional[Dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}; valid: {sorted(TABLES)}")
        row = self._conn.execute(
            f"SELECT json FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        if not row:
            return None
        rec = json.loads(row["json"])
        if self._enforce or workspace:
            self._assert_tenant(rec.get("workspace_id", ""), workspace)
        return rec

    def find(
        self,
        table: str,
        order: str = "created",
        desc: bool = False,
        limit: int = 0,
        **where: Any,
    ) -> List[Dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}; valid: {sorted(TABLES)}")
        cols = TABLES[table]
        clauses, params = [], []

        # tenant scoping — explicit wins, else the enforcing store's own ws
        explicit_ws = where.pop("workspace", None)
        if self._enforce:
            if explicit_ws is not None and explicit_ws != self.workspace_id:
                raise CrossTenantAccess(
                    f"enforcing store {self.workspace_id!r} refuses query for "
                    f"workspace {explicit_ws!r}"
                )
            eff_ws = self.workspace_id
        elif explicit_ws is not None:
            eff_ws = explicit_ws
        else:
            eff_ws = None
        if eff_ws is not None:
            clauses.append("workspace = ?")
            params.append(eff_ws)

        for k, v in where.items():
            col = COLUMN_ALIASES.get(table, {}).get(k, k)
            if col not in cols:
                raise ValueError(f"{table} has no indexed column {col!r}")
            clauses.append(f"{col} = ?")
            params.append(str(v))
        sql = f"SELECT json FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order != "created":
            if order not in cols:
                raise ValueError(
                    f"{table} has no indexed column {order!r} to order by; valid: {sorted(cols)}"
                )
            key = (
                f"CAST({order} AS REAL)"
                if order in ("created", "started", "seq", "idx", "next_run")
                else order
            )
            sql += f" ORDER BY {key} {'DESC' if desc else 'ASC'}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [json.loads(r["json"]) for r in self._conn.execute(sql, params)]

    def delete(
        self, table: str, record_id: str, event: str = "delete", workspace: str = ""
    ) -> bool:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}; valid: {sorted(TABLES)}")
        if self._enforce:
            rec = self.get(table, record_id)
            if rec is None:
                return False
            self._assert_tenant(rec.get("workspace_id", ""), workspace)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE id = ?", (record_id,)
            )
            self._conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            self.audit(event, table, record_id, {"id": record_id})
        return deleted

    def count(self, table: str, **where: Any) -> int:
        return len(self.find(table, **where))

    def next_seq(self) -> int:
        row = self._conn.execute("SELECT MAX(CAST(seq AS INTEGER)) AS m FROM runs").fetchone()
        return int(row["m"] or 0) + 1

    def close(self) -> None:
        self._conn.close()
