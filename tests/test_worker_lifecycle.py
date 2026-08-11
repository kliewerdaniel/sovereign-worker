"""§26 worker lifecycle — file-level operations fail closed.

Covers enable/disable, clone (no-clobber), archive (relocate, not delete),
export/import (portable YAML, no-clobber), and version snapshots. Also covers
the engine refusing to run a disabled worker.
"""

import os
import sys

import pytest

from sworker.config import Workspace, default_workspace, load_worker
from sworker import lifecycle as L
from sworker.engine import WorkerEngine
from sworker.store import WorkerStore


@pytest.fixture
def ws(tmp_path):
    os.environ["SWORKER_HOME"] = str(tmp_path)
    os.environ.pop("SWORKER_WORKERS_DIR", None)
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    w = default_workspace()
    os.makedirs(w.workers_dir, exist_ok=True)
    os.makedirs(w.state_dir, exist_ok=True)
    yield w
    os.environ.pop("SWORKER_HOME", None)


def _write_worker(ws, name, **over):
    import yaml
    data = {
        "name": name,
        "role": "analyst",
        "policy": {"read": "auto", "reversible": "auto", "external": "approve", "financial": "approve", "destructive": "approve"},
        "goal": "test",
        "tools": ["data.query"],
    }
    data.update(over)
    p = os.path.join(ws.workers_dir, f"{name}.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, sort_keys=False, stream=fh)
    return load_worker(p, ws)


def test_disable_then_enable(ws):
    _write_worker(ws, "alpha")
    w = L.set_enabled(ws, "alpha", disabled=True)
    assert w.disabled is True
    assert "disabled: true" in open(w.path, encoding="utf-8").read()
    w2 = L.set_enabled(ws, "alpha", disabled=False)
    assert w2.disabled is False


def test_clone_no_clobber(ws):
    _write_worker(ws, "alpha")
    L.clone(ws, "alpha", "beta")
    assert os.path.exists(os.path.join(ws.workers_dir, "beta.yaml"))
    with pytest.raises(FileExistsError):
        L.clone(ws, "alpha", "beta")
    # force overwrites
    L.clone(ws, "alpha", "beta", force=True)


def test_archive_relocates_not_deletes(ws):
    _write_worker(ws, "alpha")
    dest = L.archive(ws, "alpha")
    assert os.path.exists(dest)
    assert not os.path.exists(os.path.join(ws.workers_dir, "alpha.yaml"))
    # archived dir lives under workers_dir
    assert "archived" in dest


def test_archive_missing_fails_closed(ws):
    with pytest.raises(FileNotFoundError):
        L.archive(ws, "ghost")


def test_export_import_roundtrip(ws):
    _write_worker(ws, "alpha")
    export_path = os.path.join(ws.workers_dir, "alpha.export.yaml")
    L.export_worker(ws, "alpha", export_path)
    body = open(export_path, encoding="utf-8").read()
    assert "path:" not in body  # internal path stripped
    # import under a new name
    _write_worker(ws, "alpha")  # ensure alpha present
    with pytest.raises(FileExistsError):
        # re-importing 'alpha' must not clobber without force
        L.import_worker(ws, export_path)
    w = L.import_worker(ws, export_path, force=True)
    assert w.name == "alpha"


def test_version_snapshots_recorded(ws):
    _write_worker(ws, "alpha")
    L.set_enabled(ws, "alpha", disabled=True)
    vs = L.list_versions(ws, "alpha")
    assert len(vs) >= 1  # snapshot taken before mutation


def test_engine_refuses_disabled_worker(ws):
    _write_worker(ws, "alpha", disabled=True)
    from sworker.config import get_worker
    worker = get_worker("alpha", ws)
    engine = WorkerEngine(worker, WorkerStore(ws.state_dir))
    with pytest.raises(RuntimeError):
        engine.run("do something")
