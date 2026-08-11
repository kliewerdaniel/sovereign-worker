"""Role-based access control (spec §5).

Granular, server-side, fail-closed. A *permission* is a capability string such
as ``run:create``, ``approval:decide``, ``worker:manage``, ``audit:read``.
Roles grant sets of permissions; a user carries exactly one role (linkage set
in ``auth.User.role``).

Enforcement rule (never trust the client):
- ``authorize(role, perm)`` returns False unless the role explicitly grants the
  permission. Unknown roles and unknown permissions both deny.
- The default ``admin`` role grants everything; ``viewer`` grants read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

# Canonical permission vocabulary. Centralised so the UI/CLI and the engine
# never disagree about what a capability string means.
PERMISSIONS: Set[str] = {
    "run:create",
    "run:read",
    "approval:decide",
    "approval:read",
    "worker:manage",
    "worker:read",
    "knowledge:manage",
    "audit:read",
    "schedule:manage",
    "user:manage",
    "policy:manage",
    "secret:read",
    "secret:manage",
    "procedure:publish",
}

DEFAULT_ROLES: Dict[str, List[str]] = {
    "admin": sorted(PERMISSIONS),
    "operator": [
        "run:create", "run:read", "approval:decide", "approval:read",
        "worker:read", "schedule:manage", "procedure:publish", "audit:read",
    ],
    "analyst": ["run:create", "run:read", "approval:read", "worker:read", "audit:read"],
    "viewer": ["run:read", "approval:read", "worker:read", "audit:read"],
}


@dataclass
class Role:
    name: str
    grants: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.name, "name": self.name, "grants": sorted(self.grants)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Role":
        return cls(name=d["name"], grants=set(d.get("grants", [])))


class RBAC:
    """In-memory role registry with fail-closed authorization."""

    def __init__(self, roles: Dict[str, List[str]] | None = None):
        base = {k: list(v) for k, v in DEFAULT_ROLES.items()}
        for name, grants in (roles or {}).items():
            base[name] = [g for g in grants if g in PERMISSIONS]
        self._roles: Dict[str, Role] = {
            n: Role(name=n, grants=set(g)) for n, g in base.items()
        }

    def add_role(self, name: str, grants: List[str]) -> None:
        # only known permission strings may be granted; unknown ones are dropped
        # (fail-closed: never silently invent a capability)
        self._roles[name] = Role(name=name, grants={g for g in grants if g in PERMISSIONS})

    def role(self, name: str) -> Role | None:
        return self._roles.get(name)

    def authorize(self, role: str, permission: str) -> bool:
        r = self._roles.get(role)
        if r is None:
            return False
        # the wildcard "*" grants everything within a role's scope
        return (permission in r.grants) or ("*" in r.grants)

    def grants_for(self, role: str) -> Set[str]:
        r = self._roles.get(role)
        return set(r.grants) if r else set()

    def roles(self) -> List[Role]:
        return list(self._roles.values())


# Escalation ladder (spec §45). Ordered least -> most privileged. A role may
# only authorize an approval whose required minimum role is at or below it on
# this ladder. Unknown roles are treated as the bottom (fail-closed: never
# silently grant a privilege you cannot name).
ROLE_LADDER = ["viewer", "analyst", "worker", "operator", "admin"]


def role_rank(role: str) -> int:
    try:
        return ROLE_LADDER.index(role)
    except ValueError:
        return -1  # unknown role: below everything except itself


def role_satisfies(role: str, minimum: str) -> bool:
    """True if `role` meets or exceeds the required `minimum` role."""
    if not minimum:
        return True  # no requirement -> anyone authorized may vote
    if minimum not in ROLE_LADDER:
        # an unknown minimum is treated as max string, satisfiable only by admin
        return role == "admin"
    return role_rank(role) >= role_rank(minimum)
