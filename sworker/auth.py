"""Local-first authentication (spec §4).

No third-party crypto: passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib
``hashlib``), sessions are random 32-byte tokens, and everything is persisted
through the workspace store so it never leaves the machine.

Design invariants (fail-closed):
- A missing user, a disabled user, or a wrong password all return the SAME
  generic failure — no user enumeration.
- A session is invalid if it does not exist, is revoked, or has expired.
- Password verification is constant-time (``hmac.compare_digest``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

PBKDF2_ROUNDS = 200_000
TOKEN_BYTES = 32
DEFAULT_SESSION_TTL = 60 * 60 * 12  # 12 hours


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Return ``salt$rounds$hexhash``. A fresh salt is minted when None."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"{salt.hex()}${PBKDF2_ROUNDS}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, rounds_s, hash_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds_s))
    return hmac.compare_digest(dk.hex(), hash_hex)


@dataclass
class User:
    username: str
    pw_hash: str = ""
    salt: str = ""
    disabled: bool = False
    created: float = field(default_factory=time.time)
    role: str = "worker"  # links to an RBAC role (spec §5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.username,
            "username": self.username,
            "pw_hash": self.pw_hash,
            "disabled": self.disabled,
            "created": self.created,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "User":
        return cls(
            username=d["username"],
            pw_hash=d.get("pw_hash", ""),
            disabled=bool(d.get("disabled", False)),
            created=float(d.get("created", time.time())),
            role=d.get("role", "worker"),
        )


@dataclass
class Session:
    token: str
    username: str
    created: float = field(default_factory=time.time)
    expires: float = field(default_factory=lambda: time.time() + DEFAULT_SESSION_TTL)
    revoked: bool = False

    def is_valid(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (not self.revoked) and (self.expires > now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.token,
            "token": self.token,
            "username": self.username,
            "created": self.created,
            "expires": self.expires,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        return cls(
            token=d["token"],
            username=d["username"],
            created=float(d.get("created", time.time())),
            expires=float(d.get("expires", time.time())),
            revoked=bool(d.get("revoked", False)),
        )


class AuthProvider:
    """Store-backed local auth. Every error path is fail-closed."""

    def __init__(self, store, ttl: int = DEFAULT_SESSION_TTL):
        self.store = store
        self.ttl = ttl

    # -- users -------------------------------------------------------------
    def create_user(self, username: str, password: str, role: str = "worker") -> User:
        if not username or not password:
            raise ValueError("username and password are required")
        existing = self.get_user(username)
        if existing is not None:
            raise ValueError(f"user {username!r} already exists")
        u = User(username=username, pw_hash=_hash_password(password), role=role)
        self.store.put("users", u.to_dict(), event="user.created")
        return u

    def get_user(self, username: str) -> Optional[User]:
        rec = self.store.get("users", username)
        return User.from_dict(rec) if rec else None

    def set_password(self, username: str, new_password: str) -> None:
        u = self.get_user(username)
        if u is None:
            raise KeyError(f"no user {username!r}")
        u.pw_hash = _hash_password(new_password)
        self.store.put("users", u.to_dict(), event="user.password_changed")

    def disable_user(self, username: str) -> None:
        u = self.get_user(username)
        if u is None:
            raise KeyError(f"no user {username!r}")
        u.disabled = True
        self.store.put("users", u.to_dict(), event="user.disabled")

    def enable_user(self, username: str) -> None:
        u = self.get_user(username)
        if u is None:
            raise KeyError(f"no user {username!r}")
        u.disabled = False
        self.store.put("users", u.to_dict(), event="user.enabled")

    def list_users(self) -> list[User]:
        return [User.from_dict(r) for r in self.store.find("users", order="created")]

    # -- authentication ----------------------------------------------------
    def authenticate(self, username: str, password: str) -> Optional[Session]:
        """Return a fresh session on success, or None on any failure.

        Deliberately does not distinguish "no such user" from "wrong password"
        from "disabled" — all are None (anti-enumeration).
        """
        u = self.get_user(username)
        if u is None or u.disabled or not u.pw_hash:
            # still burn PBKDF2 cycles to keep timing roughly uniform
            _hash_password(password or "x")
            return None
        if not _verify_password(password, u.pw_hash):
            return None
        return self.create_session(username)

    # -- sessions -----------------------------------------------------------
    def create_session(self, username: str, ttl: Optional[int] = None) -> Session:
        ttl = ttl if ttl is not None else self.ttl
        tok = secrets.token_urlsafe(TOKEN_BYTES)
        s = Session(token=tok, username=username, expires=time.time() + ttl)
        self.store.put("sessions", s.to_dict(), event="session.created")
        return s

    def get_session(self, token: str) -> Optional[Session]:
        if not token:
            return None
        rec = self.store.get("sessions", token)
        return Session.from_dict(rec) if rec else None

    def validate_session(self, token: str, now: Optional[float] = None) -> Optional[str]:
        """Return the username for a valid session, else None (fail-closed)."""
        s = self.get_session(token)
        if s is None or not s.is_valid(now):
            return None
        return s.username

    def revoke_session(self, token: str) -> None:
        s = self.get_session(token)
        if s is None:
            return
        s.revoked = True
        self.store.put("sessions", s.to_dict(), event="session.revoked")

    def revoke_all(self, username: str) -> None:
        for s in self.store.find("sessions", username=username):
            sess = Session.from_dict(s)
            if sess.is_valid():
                sess.revoked = True
                self.store.put("sessions", sess.to_dict(), event="session.revoked")
