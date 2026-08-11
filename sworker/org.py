"""Organization + Workspace tenant model (spec §3).

A client must never accidentally access another client's data. Every persistent
object belonging to a workspace carries an explicit tenant identifier
(``org_id`` + ``workspace_id``), and the store refuses any read/write that
crosses it. This is defense-in-depth: the filesystem root is ONE isolation layer;
the tenant id is an INDEPENDENT one, exactly as the spec requires ("do not rely
solely on filesystem paths for tenant isolation").

    Organization
        └── Workspace        (root = <workspace>/.state)
                └── Workers / Knowledge / Runs / Artifacts
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CrossTenantAccess(Exception):
    """Raised when a store operation would cross a workspace boundary.

    Must be raised, never swallowed — this is a security boundary, not a
    convenience error.
    """


@dataclass
class Organization:
    id: str
    name: str
    created: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "created": round(self.created, 3)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Organization":
        return cls(id=d["id"], name=d["name"], created=d.get("created", time.time()))


@dataclass
class Workspace:
    id: str
    org_id: str
    name: str
    root: str
    created: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "name": self.name,
            "root": os.path.abspath(self.root),
            "created": round(self.created, 3),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Workspace":
        return cls(
            id=d["id"],
            org_id=d["org_id"],
            name=d["name"],
            root=d["root"],
            created=d.get("created", time.time()),
        )


class TenantRegistry:
    """Maps org/workspace metadata. Backed by one JSON file to keep the core's
    zero-third-party-dependency promise (no extra database required)."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._orgs: Dict[str, Organization] = {}
        self._workspaces: Dict[str, Workspace] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        for o in data.get("orgs", []):
            self._orgs[o["id"]] = Organization.from_dict(o)
        for w in data.get("workspaces", []):
            self._workspaces[w["id"]] = Workspace.from_dict(w)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {
            "orgs": [o.to_dict() for o in self._orgs.values()],
            "workspaces": [w.to_dict() for w in self._workspaces.values()],
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- orgs --------------------------------------------------------------
    def create_org(self, name: str, org_id: str = "") -> Organization:
        oid = org_id or new_id("org")
        if oid in self._orgs:
            raise ValueError(f"organization {oid!r} already exists")
        org = Organization(id=oid, name=name)
        self._orgs[oid] = org
        self._save()
        return org

    def get_org(self, org_id: str) -> Optional[Organization]:
        return self._orgs.get(org_id)

    def list_orgs(self) -> List[Organization]:
        return list(self._orgs.values())

    # -- workspaces --------------------------------------------------------
    def create_workspace(
        self, org_id: str, name: str, root: str, ws_id: str = ""
    ) -> Workspace:
        if org_id not in self._orgs:
            raise KeyError(f"no organization {org_id!r}")
        wid = ws_id or new_id("ws")
        if wid in self._workspaces:
            raise ValueError(f"workspace {wid!r} already exists")
        ws = Workspace(id=wid, org_id=org_id, name=name, root=os.path.abspath(root))
        self._workspaces[wid] = ws
        self._save()
        return ws

    def get_workspace(self, ws_id: str) -> Optional[Workspace]:
        return self._workspaces.get(ws_id)

    def list_workspaces(self, org_id: str = "") -> List[Workspace]:
        if org_id:
            return [w for w in self._workspaces.values() if w.org_id == org_id]
        return list(self._workspaces.values())

    def resolve_root(self, ws_id: str) -> str:
        w = self._workspaces.get(ws_id)
        if not w:
            raise KeyError(f"no workspace {ws_id!r}")
        return w.root
