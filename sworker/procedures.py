"""Procedural memory.

A Procedure is a YAML file: named, versioned, diffable, reviewable. It is NOT a
saved transcript — it is a structured program of steps with typed inputs and
deterministic verification checks attached.

``learn_from_run`` is the "learn how I do this" path: it reads a completed run's
ACTUAL executed actions from the ledger and generalises the literal values that
came from the task inputs back into ``{{placeholders}}``. It never invents a
step that was not really executed.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .config import WorkerConfig, Workspace, default_workspace, parse_yaml
from .store import WorkerStore
from .models import now


def procedures_dir(worker: WorkerConfig) -> str:
    d = os.path.join(worker.workspace, "procedures")
    os.makedirs(d, exist_ok=True)
    return d


def published_dir(worker: WorkerConfig) -> str:
    """§23 — directory holding published (reviewed, frozen) procedure versions.
    Layout: <procedures>/published/<name>/<version>.yaml + a `current` symlink
    file recording the active version."""
    d = os.path.join(procedures_dir(worker), "published")
    os.makedirs(d, exist_ok=True)
    return d


def _published_name_dir(worker: WorkerConfig, name: str) -> str:
    d = os.path.join(published_dir(worker), name)
    os.makedirs(d, exist_ok=True)
    return d


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_procedure_version(worker: WorkerConfig, name: str) -> str:
    """§23 — next semver-ish version: major.minor. Reads existing published
    versions for `name` and increments. First publish is 1.0."""
    d = _published_name_dir(worker, name)
    vers = []
    for fn in os.listdir(d):
        if fn.endswith(".yaml"):
            try:
                vers.append([int(x) for x in fn[:-len(".yaml")].split(".")])
            except ValueError:
                continue
    if not vers:
        return "1.0"
    vers.sort()
    major, minor = vers[-1]
    return f"{major}.{minor + 1}"


def list_published(worker: WorkerConfig) -> List[Dict[str, Any]]:
    """§23 — all published procedure versions across names."""
    out = []
    base = published_dir(worker)
    for name in sorted(os.listdir(base)):
        nd = os.path.join(base, name)
        if not os.path.isdir(nd):
            continue
        for fn in sorted(os.listdir(nd)):
            if not fn.endswith(".yaml"):
                continue
            version = fn[: -len(".yaml")]
            p = os.path.join(nd, fn)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    body = fh.read()
            except OSError:
                continue
            out.append({
                "name": name,
                "version": version,
                "path": p,
                "author": _published_meta(body).get("published_by", ""),
                "hash": _sha256_text(body),
            })
    return out


def _published_meta(body: str) -> Dict[str, Any]:
    # The meta is stored as leading `# key: value` comment lines (so it survives
    # re-parsing the procedure as YAML without polluting the procedure schema).
    out: Dict[str, Any] = {}
    for line in body.splitlines():
        if not line.startswith("#"):
            break
        if ":" in line:
            k, _, v = line[1:].partition(":")
            out[k.strip()] = v.strip()
    return out


def current_version(worker: WorkerConfig, name: str) -> Optional[str]:
    """§23 — the active published version for `name` (from current.txt)."""
    p = os.path.join(_published_name_dir(worker, name), "current.txt")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        v = fh.read().strip()
    return v or None


def publish_procedure(
    worker: WorkerConfig, name: str, body: str, *,
    author: str = "", force: bool = False,
) -> Dict[str, Any]:
    """§23 — publish a procedure version.

    Fails closed: refuses to overwrite an existing version unless `force`; the
    version is computed deterministically so publishing the same content twice
    is a no-op collision (fail closed, not silent overwrite). Records author +
    a content hash so published procedures are auditable and reproducible.
    """
    if not name:
        raise ValueError("procedure name is required to publish")
    version = next_procedure_version(worker, name)
    target = os.path.join(_published_name_dir(worker, name), f"{version}.yaml")
    if os.path.exists(target) and not force:
        raise FileExistsError(
            f"version {version} of {name!r} already exists; pass force=True to overwrite"
        )
    meta = (
        f"# published_by: {author}\n"
        f"# published_at: {now()}\n"
        f"# version: {version}\n"
        f"# source_hash: {_sha256_text(body)}\n"
    )
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(meta + body if body.startswith(("name:", "---")) else meta + "name: " + name + "\n" + body)
    # point current -> this version
    cur = os.path.join(_published_name_dir(worker, name), "current.txt")
    with open(cur, "w", encoding="utf-8") as fh:
        fh.write(version)
    return {
        "name": name,
        "version": version,
        "path": target,
        "author": author,
        "hash": _sha256_text(body),
    }


def rollback_procedure(worker: WorkerConfig, name: str, version: str = "") -> Dict[str, Any]:
    """§23 — make `version` the active version. If no version given, roll back
    to the previous one (sorted). Fails closed if the version does not exist."""
    nd = _published_name_dir(worker, name)
    avail = sorted(
        fn[:-len(".yaml")] for fn in os.listdir(nd)
        if fn.endswith(".yaml")
    )
    if not avail:
        raise ValueError(f"no published versions of {name!r} to roll back")
    if version:
        if version not in avail:
            raise ValueError(f"version {version!r} of {name!r} not found (have: {avail})")
        target = version
    else:
        current = current_version(worker, name)
        if current is None:
            raise ValueError(f"no current version of {name!r} to roll back from")
        idx = avail.index(current) if current in avail else len(avail)
        if idx <= 0:
            raise ValueError(f"already at earliest version {current!r} of {name!r}")
        target = avail[idx - 1]
    with open(os.path.join(nd, "current.txt"), "w", encoding="utf-8") as fh:
        fh.write(target)
    return {"name": name, "version": target, "rolled_back": True}


def procedure_published(worker: WorkerConfig, name: str) -> Optional[Dict[str, Any]]:
    """§23 — load the currently-active published version of `name`."""
    version = current_version(worker, name)
    if not version:
        return None
    p = os.path.join(_published_name_dir(worker, name), f"{version}.yaml")
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        data = parse_yaml(fh.read())
    if isinstance(data, dict):
        data.setdefault("name", name)
        data["path"] = p
        data["version"] = version
        return data
    return None


def can_publish(rbac, role: str) -> bool:
    """§23 — RBAC gate: publishing requires `procedure:publish`."""
    return rbac.authorize(role, "procedure:publish")



def list_procedures(worker: WorkerConfig) -> List[Dict[str, Any]]:
    out = []
    d = procedures_dir(worker)
    for name in sorted(os.listdir(d)):
        if not name.endswith((".yaml", ".yml")):
            continue
        proc = load_procedure(worker, os.path.splitext(name)[0])
        if proc:
            out.append(proc)
    return out


def load_procedure(worker: WorkerConfig, name: str) -> Optional[Dict[str, Any]]:
    base = procedures_dir(worker)
    for cand in (name, f"{name}.yaml", f"{name}.yml"):
        p = cand if os.path.isabs(cand) else os.path.join(base, cand)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as fh:
                data = parse_yaml(fh.read())
            if isinstance(data, dict):
                data.setdefault("name", os.path.splitext(os.path.basename(p))[0])
                data["path"] = p
                return data
    return None


def save_procedure(worker: WorkerConfig, name: str, body: str) -> str:
    path = os.path.join(procedures_dir(worker), f"{name}.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


# ---------------------------------------------------------------------------
# expansion
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def substitute(value: Any, inputs: Dict[str, Any]) -> Any:
    """Replace {{name}} placeholders. A whole-string placeholder keeps the
    input's native type (so {{limit}} stays an int)."""
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value.strip())
        if m:
            return inputs.get(m.group(1), value)
        return _PLACEHOLDER.sub(lambda mm: str(inputs.get(mm.group(1), mm.group(0))), value)
    if isinstance(value, dict):
        return {k: substitute(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, inputs) for v in value]
    return value


def procedure_steps(proc: Dict[str, Any], inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand a procedure into engine steps, applying declared input defaults."""
    merged: Dict[str, Any] = {}
    for spec in proc.get("inputs") or []:
        if isinstance(spec, dict):
            for key, meta in spec.items():
                if isinstance(meta, dict) and "default" in meta:
                    merged[key] = meta["default"]
                elif not isinstance(meta, dict):
                    merged[key] = meta
        elif isinstance(spec, str):
            merged.setdefault(spec, "")
    merged.update(inputs or {})

    steps: List[Dict[str, Any]] = []
    for raw in proc.get("steps") or []:
        if isinstance(raw, str):
            steps.append({"description": raw, "tool": "", "args": {}})
            continue
        if not isinstance(raw, dict):
            continue
        step = substitute(dict(raw), merged)
        steps.append(
            {
                "description": str(step.get("description") or step.get("name") or step.get("tool") or ""),
                "tool": str(step.get("tool") or ""),
                "args": step.get("args") or {},
            }
        )
    return steps


def procedure_verifications(proc: Dict[str, Any], inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for v in proc.get("verification") or proc.get("verifications") or []:
        if isinstance(v, dict):
            out.append(substitute(dict(v), inputs or {}))
    return out


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------


def learn_from_run(
    store: WorkerStore, run_id: str, name: str, *, inputs: Optional[Dict[str, Any]] = None
) -> str:
    """Turn a real, completed run into a reusable procedure.

    Only actions that ACTUALLY EXECUTED are included. Literal values matching a
    task input are generalised back into placeholders so the procedure is
    reusable rather than a one-off replay.
    """
    run = store.get("runs", run_id)
    if not run:
        raise KeyError(f"no run {run_id!r}")
    task = store.get("tasks", run["task_id"]) or {}
    known_inputs: Dict[str, Any] = dict(task.get("inputs") or {})
    known_inputs.update(inputs or {})
    reverse = {str(v): k for k, v in known_inputs.items() if str(v)}

    def generalise(value: Any) -> Any:
        if isinstance(value, str):
            if value in reverse:
                return "{{%s}}" % reverse[value]
            for lit, key in reverse.items():
                if lit and lit in value:
                    value = value.replace(lit, "{{%s}}" % key)
            return value
        if isinstance(value, dict):
            return {k: generalise(v) for k, v in value.items()}
        if isinstance(value, list):
            return [generalise(v) for v in value]
        return value

    actions = [
        a
        for a in store.find("actions", run_id=run_id, order="created")
        if a["status"] == "EXECUTED"
    ]
    if not actions:
        raise ValueError(
            f"run {run_id} executed no actions; there is no procedure to learn "
            "(refusing to write a procedure that was never demonstrated)"
        )

    lines = [
        f"name: {name}",
        f"intent: {task.get('request', '').strip() or name}",
        f"learned_from_run: {run_id}",
        "trigger:",
        "  type: manual",
    ]
    if known_inputs:
        lines.append("inputs:")
        for k, v in known_inputs.items():
            lines.append(f"  - {k}:")
            lines.append(f"      default: {v!r}")
    lines.append("steps:")
    for a in actions:
        step = store.get("steps", a["step_id"]) or {}
        desc = generalise(step.get("description") or a["tool"])
        lines.append(f"  - description: {desc!r}")
        lines.append(f"    tool: {a['tool']}")
        args = generalise(a.get("args") or {})
        if args:
            lines.append("    args:")
            for k, v in args.items():
                lines.append(f"      {k}: {_yaml_scalar(v)}")

    vers = store.find("verifications", run_id=run_id)
    if vers:
        lines.append("verification:")
        for v in vers:
            lines.append(f"  - check: {v['check']}")

    return "\n".join(lines) + "\n"


def _yaml_scalar(v: Any) -> str:
    import json as _json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        return _json.dumps(v)
    return _json.dumps(str(v))
