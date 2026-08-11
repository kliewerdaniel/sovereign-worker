"""Local authentication tests (spec §4)."""

import time

import pytest

from sworker.auth import AuthProvider, User, _hash_password, _verify_password
from sworker.store import WorkerStore


@pytest.fixture
def store():
    import tempfile, os

    d = tempfile.mkdtemp(prefix="sworker-auth-")
    return WorkerStore(os.path.join(d, "store.db"))


@pytest.fixture
def auth(store):
    return AuthProvider(store)


def test_password_hashing_is_salted_and_verifiable():
    h1 = _hash_password("hunter2")
    h2 = _hash_password("hunter2")
    assert h1 != h2  # unique salt each time
    assert _verify_password("hunter2", h1)
    assert _verify_password("hunter2", h2)
    assert not _verify_password("wrong", h1)


def test_create_and_authenticate(auth):
    u = auth.create_user("alice", "s3cret")
    assert u.username == "alice"
    sess = auth.authenticate("alice", "s3cret")
    assert sess is not None
    assert sess.username == "alice"
    assert auth.validate_session(sess.token) == "alice"


def test_wrong_password_is_none(auth):
    auth.create_user("alice", "s3cret")
    assert auth.authenticate("alice", "nope") is None
    assert auth.validate_session("bogus-token") is None


def test_missing_user_does_not_enumerate(auth):
    # must not raise; must return None like a wrong password
    assert auth.authenticate("ghost", "anything") is None


def test_disabled_user_cannot_authenticate(auth):
    auth.create_user("bob", "pw")
    auth.disable_user("bob")
    assert auth.authenticate("bob", "pw") is None
    # but the record still exists
    assert auth.get_user("bob").disabled is True


def test_session_expiry(auth):
    auth.create_user("alice", "pw")
    sess = auth.create_session("alice", ttl=10)
    assert auth.validate_session(sess.token, now=time.time() + 5) == "alice"
    assert auth.validate_session(sess.token, now=time.time() + 9999) is None


def test_session_revocation(auth):
    auth.create_user("alice", "pw")
    sess = auth.create_session("alice")
    auth.revoke_session(sess.token)
    assert auth.validate_session(sess.token) is None


def test_revoke_all_sessions(auth):
    auth.create_user("alice", "pw")
    s1 = auth.create_session("alice")
    s2 = auth.create_session("alice")
    auth.revoke_all("alice")
    assert auth.validate_session(s1.token) is None
    assert auth.validate_session(s2.token) is None


def test_set_password_and_change(auth):
    auth.create_user("alice", "old")
    auth.set_password("alice", "new")
    assert auth.authenticate("alice", "old") is None
    assert auth.validate_session(auth.authenticate("alice", "new").token) == "alice"


def test_store_roundtrip_user(store):
    u = User(username="carol", pw_hash="x", role="admin")
    store.put("users", u.to_dict(), event="user.created")
    got = User.from_dict(store.get("users", "carol"))
    assert got.role == "admin"
