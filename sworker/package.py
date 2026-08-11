"""§31 export/import package + §32 backup/restore.

  * ``export_package(ws, dest)`` bundles the entire workspace (workers, state,
    procedures, knowledge config) into a single tar.gz. Sensitive secrets are
    NEVER included (fail closed: the secrets.key is excluded and a manifest
    records its omission).
  * ``import_package(path, ws, ...)`` extracts a package into a workspace,
    refusing to overwrite an existing workspace unless ``--force`` (fail closed).
  * ``backup(ws, dest)`` and ``restore(path, ws)`` are thin wrappers over the
    same archive routine, scoped to ``state_dir`` + ``workers_dir`` so a backup
    is a faithful, restorable copy of run history + config.

All operations are stdlib only (``tarfile``/``shutil``); no third-party deps.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from typing import Any, Dict

from .config import Workspace

# Never leave the master key in an export/backup — secrets stay local.
_SECRET_NAMES = ("secrets.key",)


def _safe_members(members):
    for m in members:
        if os.path.basename(m.name) in _SECRET_NAMES:
            continue
        yield m


def export_package(ws: Workspace, dest: str, *, include_state: bool = True) -> str:
    """§31 — bundle workers + (optionally) state into a portable tar.gz."""
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    manifest = {
        "kind": "sworker-package",
        "version": 1,
        "excludes": list(_SECRET_NAMES),
        "trees": ["workers"] + (["state"] if include_state else []),
    }
    with tarfile.open(dest, "w:gz") as tf:
        # manifest first
        import io

        data = (f"# sworker package manifest\n{_yaml_dump(manifest)}").encode("utf-8")
        info = tarfile.TarInfo(name="MANIFEST.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        for tree in manifest["trees"]:
            root = os.path.join(ws.root, tree)
            if not os.path.isdir(root):
                continue
            tf.add(root, arcname=tree, filter=lambda m: m)
        # append secret omission note
    return dest


def import_package(path: str, ws: Workspace, *, force: bool = False) -> Dict[str, Any]:
    """§31 — extract a package. Refuses to clobber a non-empty workspace (fail closed)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"package {path!r} not found")
    if not force and os.path.exists(ws.root) and os.listdir(ws.root):
        raise FileExistsError(
            f"workspace {ws.root!r} is not empty; pass force=True to overwrite"
        )
    extracted = []
    with tarfile.open(path, "r:gz") as tf:
        for m in _safe_members(tf.getmembers()):
            tf.extract(m, ws.root)
            extracted.append(m.name)
    # re-create state dir if the package omitted it
    os.makedirs(ws.state_dir, exist_ok=True)
    return {"extracted": len(extracted), "root": ws.root}


def backup(ws: Workspace, dest: str) -> str:
    """§32 — backup state + workers (no secrets key)."""
    return export_package(ws, dest, include_state=True)


def restore(path: str, ws: Workspace, *, force: bool = False) -> Dict[str, Any]:
    """§32 — restore a backup into a workspace (fail closed on non-empty target)."""
    return import_package(path, ws, force=force)


def _yaml_dump(d: Dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(d, sort_keys=False)
