"""Phase 1 cross-subsystem integration (auth+rbac+policy+secrets+audit)."""

import os

from sworker.store import WorkerStore
from sworker.auth import AuthProvider
from sworker.rbac import RBAC
from sworker.policy import PolicyStore
from sworker.secrets import SecretStore


def _ws():
    import tempfile

    d = tempfile.mkdtemp(prefix="sworker-int-")
    return WorkerStore(os.path.join(d, "store.db"))


def test_all_subsystems_share_store_and_audit_verifies():
    store = _ws()

    # auth + rbac
    ap = AuthProvider(store)
    ap.create_user("bob", "pw", role="operator")
    rbac = RBAC()
    assert rbac.authorize("operator", "approval:decide")
    assert not rbac.authorize("operator", "user:manage")

    # policy
    ps = PolicyStore(store)
    p = ps.publish(
        {"read": "auto", "reversible": "auto", "external": "approve",
         "financial": "approve", "destructive": "deny"},
        "workspace:acme", actor="bob",
    )

    # secrets (isolated optional dep present)
    ss = SecretStore(store, key=os.urandom(32))
    ss.set("API", "topsecret-value", actor="bob")
    assert ss.get("API") == "topsecret-value"
    # plaintext never hits the store
    rec = store.get("secrets", "API")
    assert rec is not None and "topsecret-value" not in rec["ciphertext"]

    # run persists the policy version it executed under
    run = {"id": "run_int1", "policy_hash": p.hash, "policy_version": p.version}
    store.put("runs", run, event="run.started")

    # audit chain still intact across all the writes above
    report = store.verify_audit_chain()
    assert report["ok"], report["errors"]
    assert report["checked"] > 0
