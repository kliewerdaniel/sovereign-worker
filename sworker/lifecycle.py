"""§26 worker lifecycle — clone / enable / disable / archive / export / import.

All operations are file-level and fail closed:
  * enable/disable flip a `disabled` flag in the worker YAML (the engine refuses
    to run disabled workers).
  * clone copies a worker to a NEW name; refuses to clobber an existing worker.
  * archive moves the worker file into `archived/` (out of the active workers
    dir, so it can no longer be loaded) but keeps it on disk for audit/restore.
  * export writes a self-contained YAML (no path) to a file; import refuses to
    overwrite an existing worker unless `--force`.
  * A simple version history under `workers/versions/<name>/` records each
    mutation so lifecycle changes are auditable and reversible.

No third-party deps. Operations on the filesystem only — they never touch
running runs.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

from .config import (
    Workspace, default_workspace, get_worker, list_workers, load_worker,
    parse_yaml, WorkerConfig,
)
from .models import now


def _versions_dir(ws: Workspace) -> str:
    d = os.path.join(ws.workers_dir, "versions")
    os.makedirs(d, exist_ok=True)
    return d


def _archive_dir(ws: Workspace) -> str:
    d = os.path.join(ws.workers_dir, "archived")
    os.makedirs(d, exist_ok=True)
    return d


def _snapshot(ws: Workspace, name: str, note: str) -> None:
    """§26 — record a version snapshot before a mutation."""
    vd = os.path.join(_versions_dir(ws), name)
    os.makedirs(vd, exist_ok=True)
    try:
        body = _read_worker_file(ws, name)
    except FileNotFoundError:
        return
    import hashlib
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    ts = int(now())
    with open(os.path.join(vd, f"{ts}-{h}.yaml"), "w", encoding="utf-8") as fh:
        fh.write(f"# version note: {note}\n# at: {ts}\n")
        fh.write(body)


def _read_worker_file(ws: Workspace, name: str) -> str:
    for ext in (".yaml", ".yml"):
        p = os.path.join(ws.workers_dir, name + ext)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()
    raise FileNotFoundError(f"no worker {name!r}")


def list_versions(ws: Workspace, name: str) -> List[str]:
    vd = os.path.join(_versions_dir(ws), name)
    if not os.path.isdir(vd):
        return []
    return sorted(os.listdir(vd))


def set_enabled(ws: Workspace, name: str, disabled: bool) -> WorkerConfig:
    """§26 — flip the disabled flag. The engine refuses to run disabled workers."""
    worker = get_worker(name, ws)
    _snapshot(ws, name, f"{'disable' if disabled else 'enable'}")
    data = worker.to_dict()
    data["disabled"] = bool(disabled)
    # write back without the internal `path`
    body = _render_worker(data)
    with open(worker.path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return load_worker(worker.path, ws)


def clone(ws: Workspace, src: str, dst: str, *, force: bool = False) -> WorkerConfig:
    """§26 — clone a worker. Refuses to clobber an existing worker (fail closed)."""
    src_worker = get_worker(src, ws)
    for ext in (".yaml", ".yml"):
        if os.path.exists(os.path.join(ws.workers_dir, dst + ext)):
            if not force:
                raise FileExistsError(
                    f"worker {dst!r} already exists; pass force=True to overwrite"
                )
    _snapshot(ws, dst, f"clone from {src}")
    data = src_worker.to_dict()
    data["name"] = dst
    data.pop("path", None)
    body = _render_worker(data)
    dst_path = os.path.join(ws.workers_dir, f"{dst}.yaml")
    with open(dst_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return load_worker(dst_path, ws)


def archive(ws: Workspace, name: str) -> str:
    """§26 — move a worker out of the active dir into `archived/`.

    Fail closed: refusing to archive a worker that is not present. The file is
    relocated (not deleted) so it can be restored, and a version snapshot is kept.
    """
    worker = get_worker(name, ws)
    _snapshot(ws, name, "archive")
    dest = os.path.join(_archive_dir(ws), os.path.basename(worker.path))
    if os.path.exists(dest):
        raise FileExistsError(f"archived copy of {name!r} already exists")
    shutil.move(worker.path, dest)
    return dest


def export_worker(ws: Workspace, name: str, dest: str) -> str:
    """§26 — export a worker as portable YAML (path stripped)."""
    worker = get_worker(name, ws)
    data = worker.to_dict()
    data.pop("path", None)
    body = _render_worker(data)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(body)
    return dest


def import_worker(ws: Workspace, src: str, *, force: bool = False) -> WorkerConfig:
    """§26 — import a worker YAML. Refuses to overwrite an existing worker
    unless `--force` (fail closed)."""
    with open(src, "r", encoding="utf-8") as fh:
        data = parse_yaml(fh.read())
    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError("imported file is not a valid worker (missing name)")
    name = data["name"]
    for ext in (".yaml", ".yml"):
        if os.path.exists(os.path.join(ws.workers_dir, name + ext)):
            if not force:
                raise FileExistsError(
                    f"worker {name!r} already exists; pass force=True to overwrite"
                )
    _snapshot(ws, name, f"import from {os.path.basename(src)}")
    body = _render_worker(data)
    dst = os.path.join(ws.workers_dir, f"{name}.yaml")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(body)
    return load_worker(dst, ws)


def _render_worker(data: Dict[str, Any]) -> str:
    """§26 — render a worker dict as clean, ordered YAML."""
    import yaml
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
