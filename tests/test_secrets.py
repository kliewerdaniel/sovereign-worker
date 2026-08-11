"""Encrypted secrets tests (spec §8) -- fail-closed, no-leak."""

import os

import pytest

from sworker.secrets import SecretStore, _fp, redact_static
from sworker.store import WorkerStore


@pytest.fixture
def store():
    import tempfile

    d = tempfile.mkdtemp(prefix="sworker-sec-")
    return WorkerStore(os.path.join(d, "store.db"))


@pytest.fixture
def ss(store):
    return SecretStore(store, key=os.urandom(32))


def test_set_and_get_roundtrip(ss):
    ss.set("API_KEY", "super-secret-value-123")
    assert ss.get("API_KEY") == "super-secret-value-123"


def test_plaintext_never_persisted(store):
    ss = SecretStore(store, key=os.urandom(32))
    ss.set("TOKEN", "do-not-leak-xyz")
    # the store record contains no plaintext token
    rec = store.get("secrets", "TOKEN")
    assert "do-not-leak-xyz" not in rec["ciphertext"]
    assert "do-not-leak-xyz" not in rec["fingerprint"]
    assert "do-not-leak-xyz" not in rec["name"]


def test_value_is_ciphertext_not_clear(ss):
    ss.set("P", "plaintext-should-be-hidden")
    rec = ss.store.get("secrets", "P")
    nonce_and_ct = rec["ciphertext"]
    assert "plaintext-should-be-hidden" != nonce_and_ct


def test_fingerprint_deterministic_and_not_value(ss):
    ss.set("X", "abc")
    fp = _fp("abc")
    rec = ss.store.get("secrets", "X")
    assert rec["fingerprint"] == fp
    assert fp != "abc"


def test_redact_replaces_known_values(ss):
    ss.set("DB_PASS", "hunter2-very-secret")
    out = ss.redact("connect using hunter2-very-secret and done")
    assert "hunter2-very-secret" not in out
    assert "***REDACTED***" in out


def test_static_redact_patterns():
    txt = 'api_key="sk_live_abc123DEF456" token: xyz789ABCDEF12'
    out = redact_static(txt)
    assert "sk_live_abc123DEF456" not in out
    assert "xyz789ABCDEF12" not in out
    assert "***REDACTED***" in out


def test_delete_removes_secret(ss):
    ss.set("TMP", "v")
    ss.delete("TMP")
    assert not ss.exists("TMP")
    with pytest.raises(Exception):
        ss.get("TMP")


def test_key_derivation_symmetric():
    import tempfile

    d = tempfile.mkdtemp(prefix="sworker-kek-")
    kp = os.path.join(d, "secrets.key")
    shared_store = WorkerStore(os.path.join(d, "s.db"))
    a = SecretStore(shared_store, key_path=kp)
    a.set("K", "shared-secret")
    # a second SecretStore from the same key file can read the ciphertext
    b = SecretStore(shared_store, key_path=kp)
    assert b.get("K") == "shared-secret"
