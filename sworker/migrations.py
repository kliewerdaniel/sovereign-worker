"""§60 — data migration framework (forward-compatible, fail-closed).

Stored worker state (sqlite index + audit log + config YAML) evolves across
releases. This module lets a workspace be upgraded from whatever version it was
written with to the current one, without re-creating it and without losing the
audit trail.

Design rules (mirror the platform's fail-closed posture):

* Migrations are **ordered, additive, idempotent** steps. Migration `N` upgrades
  data *from* version `N` *to* version `N+1`. There is no down-migration — a
  downgrade is never silently attempted.
* Every applied step is recorded **both** in the `meta` table (so re-running is
  a no-op) **and** in the append-only audit log (so the upgrade is itself
  auditable and tamper-evident).
* A target version below the current, or above the highest registered migration,
  is refused — we never guess, never skip, never roll back.
* The framework is pure stdlib; it imports nothing third-party and is safe to
  import from `cli`, `web`, `engine`, or a bare test.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Tuple

from .store import WorkerStore

# Logical *data* version. Distinct from the sqlite `schema_version`
# (structural DDL), because data format can change without a schema change and
# vice versa. Bump `DATA_VERSION` only when a new entry is added to MIGRATIONS.
DATA_VERSION: int = 1

# version N -> (description, upgrade(store) -> None)
# upgrade must take a store *from* N *to* N+1 idempotently.
MIGRATIONS: Dict[int, Tuple[str, Callable[["WorkerStore"], None]]] = {}


def _meta_get(store: WorkerStore, key: str, default: str = "") -> str:
    cur = store._conn.cursor()
    row = cur.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def _meta_set(store: WorkerStore, key: str, value: str) -> None:
    cur = store._conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value)
    )
    store._conn.commit()


def current_version(store: WorkerStore) -> int:
    """The data version the store was last migrated to (0 == legacy)."""
    raw = _meta_get(store, "data_version", "")
    if not raw:
        # No marker yet: this is a legacy store predating the framework.
        return 0
    try:
        return int(raw)
    except ValueError:
        # A corrupted marker is a real problem; treat it as needing attention
        # rather than silently assuming a version.
        return -1


def pending(store: WorkerStore) -> List[int]:
    """Sorted list of migration versions not yet applied to this store."""
    cur = current_version(store)
    if cur < 0:
        # corrupted marker — caller must decide; report all as pending so a
        # `migrate` will refuse (target above current, but marker invalid).
        return sorted(MIGRATIONS.keys())
    return sorted(v for v in MIGRATIONS if v > cur)


def migrate(store: WorkerStore, to_version: int | None = None) -> List[int]:
    """Apply pending migrations up to ``to_version`` (default: DATA_VERSION).

    Returns the list of versions actually applied. Fail-closed:
      * ``to_version`` below the store's current version -> refuse (no rollback).
      * ``to_version`` above the highest registered migration -> refuse (never
        guess what an unregistered future step should do).
      * a corrupted version marker -> refuse.

    Each applied step is recorded in ``meta`` and in the audit log.
    """
    target = DATA_VERSION if to_version is None else to_version
    cur = current_version(store)
    if cur < 0:
        raise MigrationError(
            f"store has a corrupted data_version marker; refusing to migrate"
        )
    if target < cur:
        raise MigrationError(
            f"refusing to downgrade data from v{cur} to v{target}"
        )
    highest = max(MIGRATIONS.keys(), default=0)
    if target > highest:
        raise MigrationError(
            f"target v{target} exceeds highest registered migration v{highest}; "
            f"upgrade the platform before migrating to an unknown version"
        )
    applied: List[int] = []
    # Walk strictly in ascending order; each step moves cur -> cur+1.
    for v in sorted(MIGRATIONS.keys()):
        if v <= cur or v > target:
            continue
        desc, fn = MIGRATIONS[v]
        fn(store)
        _meta_set(store, "data_version", str(v))
        store.audit("migration", "meta", f"data_version:{v}",
                    {"from": v - 1, "to": v, "description": desc})
        applied.append(v)
    return applied


class MigrationError(RuntimeError):
    """Raised when a migration cannot / must not proceed (fail-closed)."""


# ---------------------------------------------------------------------------
# Registered migrations
# ---------------------------------------------------------------------------
# v1: baseline. The schema already carries tenant columns (TENANT_COLS) and a
# `schema_version` marker from earlier phases; this step simply stamps the data
# version marker so future upgrades have a known floor. It is a genuine,
# idempotent upgrade (no-op on the data, establishes the marker) and proves the
# framework end-to-end without fabricating history.
def _migration_1(store: WorkerStore) -> None:
    # Defensive: ensure the legacy tenant backfill has actually landed. The
    # store backfills these on open, but a partially-initialised store should
    # not advance the data version until the invariant holds.
    cur = store._conn.cursor()
    for table in ("runs", "actions", "approvals", "artifacts", "evidence",
                  "schedules", "procedures", "users", "sessions"):
        if table not in _table_names(cur):
            continue
        for col in ("org", "workspace"):
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # already present
    store._conn.commit()


MIGRATIONS[1] = ("establish data_version marker; ensure tenant columns present",
                 _migration_1)


def _table_names(cur) -> List[str]:
    rows = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return [r["name"] for r in rows]
