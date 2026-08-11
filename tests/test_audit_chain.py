"""Hash-chain audit (spec §13).

The append-only ledger gets a rolling hash chain: each event hashes its own
contents PLUS the previous event's hash. Tampering with any line — re-hashing or
not — must be detected by ``verify_audit_chain``. Pre-upgrade (hashless) lines
are tolerated as trusted genesis but the chain is still validated from the first
hashed line.
"""

from __future__ import annotations

from sworker.store import WorkerStore


def test_chain_builds_and_verifies(tmp_path):
    store = WorkerStore(str(tmp_path / "w"))
    for i in range(5):
        store.audit("evt", "runs", f"id_{i}", {"n": i})
    report = store.verify_audit_chain()
    assert report["ok"] is True, report["errors"]
    assert report["checked"] == 5
    assert report["lines"] == 5


def test_tampering_detected(tmp_path):
    store = WorkerStore(str(tmp_path / "w"))
    store.audit("evt", "runs", "id_1", {"v": 1})
    store.audit("evt", "runs", "id_2", {"v": 2})
    store.audit("evt", "runs", "id_3", {"v": 3})

    # mutate a recorded value in the middle of the file
    p = store.audit_path
    with open(p, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    rec = __import__("json").loads(lines[1])
    rec["payload"]["v"] = 999  # tamper
    lines[1] = __import__("json").dumps(rec) + "\n"
    with open(p, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    report = store.verify_audit_chain()
    assert report["ok"] is False
    assert any("altered" in e["error"] for e in report["errors"])


def test_broken_link_detected(tmp_path):
    store = WorkerStore(str(tmp_path / "w"))
    store.audit("evt", "runs", "id_1", {"v": 1})
    store.audit("evt", "runs", "id_2", {"v": 2})

    # change a record's previous_event_hash so the link is broken
    p = store.audit_path
    with open(p, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    rec = __import__("json").loads(lines[1])
    rec["previous_event_hash"] = "deadbeef"
    lines[1] = __import__("json").dumps(rec) + "\n"
    with open(p, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    report = store.verify_audit_chain()
    assert report["ok"] is False
    assert any("link broken" in e["error"] for e in report["errors"])


def test_legacy_lines_accepted(tmp_path):
    """Lines without event_hash (pre-upgrade) are tolerated."""
    store = WorkerStore(str(tmp_path / "w"))
    store.audit("evt", "runs", "id_1", {"v": 1})
    # append a legacy (hashless) line by hand
    with open(store.audit_path, "a", encoding="utf-8") as fh:
        fh.write(__import__("json").dumps({"ts": 1.0, "event": "old", "id": "x"}) + "\n")
    store.audit("evt", "runs", "id_2", {"v": 2})

    report = store.verify_audit_chain()
    assert report["ok"] is True
    assert report["checked"] == 2


def test_empty_store_verifies(tmp_path):
    store = WorkerStore(str(tmp_path / "w"))
    assert store.verify_audit_chain()["ok"] is True


def test_chain_persists_across_reopen(tmp_path):
    db = str(tmp_path / "w")
    a = WorkerStore(db)
    a.audit("evt", "runs", "id_1", {"v": 1})
    a.audit("evt", "runs", "id_2", {"v": 2})
    b = WorkerStore(db)
    assert b.verify_audit_chain()["ok"] is True
