"""Versioned, immutable policy records (spec §6).

A *policy* maps each risk category (read/reversible/external/financial/
destructive) to a disposition: ``auto`` (engine may proceed), ``approve``
(requires human approval), or ``deny`` (never allowed).

Policies are **immutable and versioned**:
- Each version is content-addressed by ``hash`` (sha256 of its canonical form),
  so two identical policies share an id and a change is always a *new* version.
- ``current`` resolves to the latest active version for a scope (workspace or
  org-wide). Promoting a version is the only mutating operation; the version
  bodies themselves are never edited or deleted.
- A run records the ``policy_hash`` + ``policy_version`` it executed under, so
  an audit years later can reproduce exactly what was allowed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RISK_CATEGORIES = ("read", "reversible", "external", "financial", "destructive")
DISPOSITIONS = ("auto", "approve", "deny")


def _canonical(body: Dict[str, str]) -> str:
    return json.dumps({k: body.get(k) for k in RISK_CATEGORIES}, sort_keys=True)


@dataclass
class Policy:
    hash: str
    version: int
    scope: str           # e.g. "workspace:acme" or "org:default"
    body: Dict[str, str]
    created: float = field(default_factory=time.time)
    actor: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.hash,
            "hash": self.hash,
            "version": self.version,
            "scope": self.scope,
            "body": dict(self.body),
            "created": self.created,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Policy":
        return cls(
            hash=d["hash"], version=int(d["version"]), scope=d["scope"],
            body=dict(d.get("body", {})), created=float(d.get("created", time.time())),
            actor=d.get("actor", "system"),
        )


def make_policy(body: Dict[str, str], scope: str, version: int = 1, actor: str = "system") -> Policy:
    canonical = _canonical(body)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return Policy(hash=h, version=version, scope=scope, body=dict(body), actor=actor)


class PolicyStore:
    """Append-only, content-addressed policy registry backed by the store."""

    def __init__(self, store):
        self.store = store

    def publish(self, body: Dict[str, str], scope: str, actor: str = "system") -> Policy:
        """Store a new (or identical) immutable version. Returns the Policy."""
        existing = self.latest(scope)
        version = (existing.version + 1) if existing else 1
        pol = make_policy(body, scope, version=version, actor=actor)
        # if an identical policy hash already exists for this scope, reuse it
        prior = self.store.get("policies", pol.hash)
        if prior and prior.get("scope") == scope:
            return Policy.from_dict(prior)
        self.store.put("policies", pol.to_dict(), event="policy.published")
        # promote to current for the scope
        self._set_current(scope, pol.hash)
        return pol

    def get(self, policy_hash: str) -> Optional[Policy]:
        rec = self.store.get("policies", policy_hash)
        return Policy.from_dict(rec) if rec else None

    def latest(self, scope: str) -> Optional[Policy]:
        h = self._current_hash(scope)
        if not h:
            return None
        return self.get(h)

    def list_versions(self, scope: str) -> List[Policy]:
        return [Policy.from_dict(r) for r in self.store.find("policies", scope=scope, order="version")]

    def _current_key(self, scope: str) -> str:
        return f"policy:current:{scope}"

    def _set_current(self, scope: str, policy_hash: str) -> None:
        self.store.put(
            "meta_kv", {"id": self._current_key(scope), "scope": scope, "policy_hash": policy_hash},
            event="policy.promoted",
        )

    def _current_hash(self, scope: str) -> Optional[str]:
        rec = self.store.get("meta_kv", self._current_key(scope))
        return rec.get("policy_hash") if rec else None
