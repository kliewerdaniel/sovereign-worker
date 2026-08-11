"""§33 doctor — local-first workspace health check.

Runs a battery of fail-closed diagnostics and returns a structured report. Each
check is independent; one failing check never masks another. Severity:
  * ``error`` — the worker cannot safely run (e.g. audit chain broken, workspace
    missing).
  * ``warn`` — degraded but runnable (e.g. optional Atlas not reachable, no
    secrets key yet).
  * ``ok`` — healthy.
The CLI surfaces this as a human table and as ``--json`` for automation.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

from .config import Workspace, default_workspace, list_workers
from .store import WorkerStore


def _check(label: str, severity_default: str = "ok") -> Dict[str, Any]:
    return {"label": label, "status": severity_default, "detail": ""}


def run_doctor(ws: Workspace) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    # 1. workspace present
    c = _check("workspace directory")
    if os.path.isdir(ws.root):
        c["detail"] = ws.root
    else:
        c["status"] = "error"
        c["detail"] = f"missing: {ws.root}"
    checks.append(c)

    # 2. workers dir
    c = _check("workers directory")
    c["detail"] = ws.workers_dir
    c["status"] = "ok" if os.path.isdir(ws.workers_dir) else "error"
    checks.append(c)

    # 3. audit chain integrity (fail closed: a broken chain is an error)
    c = _check("audit chain integrity")
    store = WorkerStore(ws.state_dir)
    rep = store.verify_audit_chain()
    if rep.get("ok"):
        c["status"] = "ok"
        c["detail"] = f"{rep.get('lines','?')} lines verified"
    else:
        c["status"] = "error"
        c["detail"] = f"broken chain: {rep.get('errors')}"
    checks.append(c)

    # 4. workers parse / none broken
    c = _check("worker configs parse")
    bad = []
    try:
        for w in list_workers(ws):
            try:
                w.to_dict()
            except Exception as exc:
                bad.append(f"{w.name}: {exc}")
    except Exception as exc:
        bad.append(f"list error: {exc}")
    if bad:
        c["status"] = "error"
        c["detail"] = "; ".join(bad)
    else:
        c["detail"] = f"{len(list_workers(ws))} worker(s) parse"
    checks.append(c)

    # 5. python version (warn if < 3.10)
    c = _check("python runtime")
    major, minor = sys.version_info[:2]
    c["detail"] = f"{major}.{minor}.{sys.version_info[2]}"
    c["status"] = "ok" if (major, minor) >= (3, 10) else "warn"

    # 6. optional Atlas reachable (warn, not error — Atlas is optional)
    c = _check("Hermes Atlas (optional)")
    try:
        import hermes_atlas  # noqa: F401

        c["status"] = "ok"
        c["detail"] = "importable"
    except Exception:
        c["status"] = "warn"
        c["detail"] = "not installed (knowledge compile disabled)"

    # 7. secrets key present (warn — secrets feature stays opt-in)
    c = _check("secrets master key")
    key_path = os.path.join(ws.state_dir, "secrets.key")
    if os.path.exists(key_path):
        c["status"] = "ok"
        c["detail"] = "present"
    else:
        c["status"] = "warn"
        c["detail"] = "absent (connector secrets encrypted on first use)"

    errors = [c for c in checks if c["status"] == "error"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {
        "ok": not errors,
        "checks": checks,
        "errors": len(errors),
        "warnings": len(warns),
    }
