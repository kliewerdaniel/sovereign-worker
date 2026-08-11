"""§60 data migration framework — fail-closed, forward-only, auditable.

The framework must: stamp a data version on legacy stores, apply pending
migrations exactly once (re-running is a no-op), record each step in the audit
log, and REFUSE a downgrade, an unknown future target, or a corrupted marker.
"""

import os
import tempfile

import pytest

from sworker import migrations as M
from sworker.store import WorkerStore
from sworker.config import Workspace


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return WorkerStore(Workspace(d).state_dir)


def test_legacy_store_starts_at_zero(store):
    # a fresh, pre-framework store has no data_version marker -> 0
    assert M.current_version(store) == 0
    assert M.pending(store) == [1]


def test_migrate_applies_and_records(store):
    applied = M.migrate(store)
    assert applied == [1]
    assert M.current_version(store) == 1
    # the upgrade is auditable
    lines = list(store.iter_audit())
    assert any(a.get("event") == "migration" for a in lines)


def test_migrate_is_idempotent(store):
    assert M.migrate(store) == [1]
    # second run must apply nothing
    assert M.migrate(store) == []
    assert M.current_version(store) == 1
    # only one migration line in the audit log
    migs = [a for a in store.iter_audit() if a.get("event") == "migration"]
    assert len(migs) == 1


def test_refuses_downgrade(store):
    M.migrate(store)
    assert M.current_version(store) == 1
    with pytest.raises(M.MigrationError):
        M.migrate(store, to_version=0)


def test_refuses_unknown_future_target(store):
    # DATA_VERSION is the highest registered; asking for a higher one is refused
    with pytest.raises(M.MigrationError):
        M.migrate(store, to_version=M.DATA_VERSION + 5)


def test_pending_empty_after_migrate(store):
    M.migrate(store)
    assert M.pending(store) == []


def test_corrupted_marker_refuses(store, monkeypatch):
    # force a garbage data_version into the meta table, then ensure migrate
    # refuses rather than guessing.
    cur = store._conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('data_version', 'not-a-number')")
    store._conn.commit()
    # current_version returns -1 for garbage
    assert M.current_version(store) == -1
    with pytest.raises(M.MigrationError):
        M.migrate(store)


def test_migrate_does_not_clobber_existing_data(store):
    # put a record, migrate, confirm it survives and the tenant columns still read
    store.put("runs", {"id": "run_1", "status": "SUCCESS",
                       "org": "o1", "workspace": "w1", "json": "{}"})
    M.migrate(store)
    rec = store.get("runs", "run_1")
    assert rec["status"] == "SUCCESS"
    assert rec["workspace"] == "w1"
    assert M.current_version(store) == 1


def test_cli_migrate_dry_run_and_apply(store, capsys, monkeypatch):
    from sworker import cli
    # the CLI opens WorkerStore(Workspace(home).state_dir); store.root is that
    # .state dir, so the workspace root is its parent.
    home = os.path.dirname(store.root)

    class _A:
        home = ""
        to = None
        dry_run = True
        json = False

    a = _A()
    a.home = home
    rc = cli.cmd_migrate(a)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending migrations" in out
    # after dry-run nothing was applied
    assert M.current_version(store) == 0
    # now apply for real
    a.dry_run = False
    rc = cli.cmd_migrate(a)
    assert rc == 0
    assert M.current_version(store) == 1
