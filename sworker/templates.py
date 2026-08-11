"""§25 worker templates — reusable scaffolds for new workers.

A *template* is a YAML recipe with a ``{name}`` placeholder for the worker name
and ``{goal}`` placeholder for its purpose. ``create_worker`` renders a template
into a concrete worker file. Templates are fail-closed: an unknown template name
raises ``TemplateError``; a render that would clobber an existing worker raises
``FileExistsError``.

The optional *marketplace* is just a directory of exported worker YAMLs
(``<ws>/marketplace/*.yaml``) — importable via the §26 lifecycle importer, so no
new machinery is needed for sharing. This module provides listing/publishing of
that directory.
"""

from __future__ import annotations

import os
from typing import Dict, List

import yaml

from .config import Workspace, load_worker

BUILTIN_TEMPLATES: Dict[str, str] = {
    "analyst": (
        "name: {name}\n"
        "role: analyst\n"
        "goal: {goal}\n"
        "policy:\n"
        "  read: auto\n"
        "  reversible: auto\n"
        "  external: approve\n"
        "  financial: approve\n"
        "  destructive: approve\n"
        "tools: [data.query]\n"
    ),
    "operator": (
        "name: {name}\n"
        "role: operator\n"
        "goal: {goal}\n"
        "policy:\n"
        "  read: auto\n"
        "  reversible: auto\n"
        "  external: approve\n"
        "  financial: approve\n"
        "  destructive: approve\n"
        "tools: [data.query, shell.exec]\n"
    ),
    "ingest": (
        "name: {name}\n"
        "role: ingester\n"
        "goal: {goal}\n"
        "policy:\n"
        "  read: auto\n"
        "  reversible: auto\n"
        "  external: approve\n"
        "  financial: approve\n"
        "  destructive: approve\n"
        "tools: [data.query, knowledge.compile]\n"
        "# add a §24 file_changed trigger with absolute roots, e.g.:\n"
        "# triggers:\n"
        "#   - kind: file_changed\n"
        "#     roots: [/abs/path/to/watch]\n"
        "#     interval: 5.0\n"
    ),
}


class TemplateError(ValueError):
    """§25 — template name unknown or render failed (fail closed)."""


def list_templates() -> List[str]:
    return sorted(BUILTIN_TEMPLATES)


def render_template(name: str, worker_name: str, goal: str) -> str:
    """§25 — render a built-in template into concrete YAML."""
    if name not in BUILTIN_TEMPLATES:
        raise TemplateError(f"unknown template {name!r}; known: {list_templates()}")
    return BUILTIN_TEMPLATES[name].format(name=worker_name, goal=goal)


def create_worker(
    ws: Workspace, template: str, worker_name: str, goal: str, *, force: bool = False
) -> "object":
    """§25 — scaffold a new worker from a template. Fail closed on unknown
    template or collision (unless force)."""
    body = render_template(template, worker_name, goal)
    # validate the rendered YAML parses before writing
    data = yaml.safe_load(body)
    if not isinstance(data, dict) or data.get("name") != worker_name:
        raise TemplateError("rendered template did not produce a valid worker")
    for ext in (".yaml", ".yml"):
        if os.path.exists(os.path.join(ws.workers_dir, worker_name + ext)):
            if not force:
                raise FileExistsError(
                    f"worker {worker_name!r} already exists; pass force=True to overwrite"
                )
    dest = os.path.join(ws.workers_dir, f"{worker_name}.yaml")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(body)
    return load_worker(dest, ws)


# --- opt-in marketplace (a directory of exported workers) -----------------


def marketplace_dir(ws: Workspace) -> str:
    d = os.path.join(ws.workers_dir, "marketplace")
    os.makedirs(d, exist_ok=True)
    return d


def list_marketplace(ws: Workspace) -> List[str]:
    d = marketplace_dir(ws)
    return sorted(
        f[: -len(".yaml")] for f in os.listdir(d) if f.endswith(".yaml")
    )


def publish_to_marketplace(ws: Workspace, worker_name: str) -> str:
    """§25 — copy a worker YAML into the marketplace dir (no path)."""
    from . import lifecycle as L

    src = os.path.join(ws.workers_dir, f"{worker_name}.yaml")
    if not os.path.exists(src):
        raise FileExistsError(f"no worker {worker_name!r} to publish")
    dest = os.path.join(marketplace_dir(ws), f"{worker_name}.yaml")
    if os.path.exists(dest):
        raise FileExistsError(f"marketplace already has {worker_name!r}")
    L.export_worker(ws, worker_name, dest)
    return dest


def import_from_marketplace(ws: Workspace, name: str, *, force: bool = False) -> "object":
    from . import lifecycle as L

    src = os.path.join(marketplace_dir(ws), f"{name}.yaml")
    if not os.path.exists(src):
        raise FileNotFoundError(f"marketplace has no {name!r}")
    return L.import_worker(ws, src, force=force)
