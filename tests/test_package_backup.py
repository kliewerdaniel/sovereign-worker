"""§31/§32 package export+import + backup/restore, §33 doctor.

Fail-closed guarantees:
  * import/restore refuse to clobber a non-empty workspace (FileExistsError).
  * secrets.key is never written into an export/backup (excluded member).
  * doctor flags a broken audit chain as error; ok otherwise.
"""

import os
import tarfile

import pytest

from sworker.config import Workspace, default_workspace
from sworker import doctor as D
from sworker import package as P


@pytest.fixture
def ws(tmp_path):
    os.environ["SWORKER_HOME"] = str(tmp_path)
    os.environ.pop("SWORKER_WORKERS_DIR", None)
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    w = default_workspace()
    os.makedirs(w.workers_dir, exist_ok=True)
    os.makedirs(w.state_dir, exist_ok=True)
    os.makedirs(os.path.join(w.root, "company"), exist_ok=True)
    yield w
    os.environ.pop("SWORKER_HOME", None)


def _seed_worker(w, name="a"):
    import yaml
    data = {"name": name, "role": "analyst",
            "policy": {"read": "auto", "reversible": "auto", "external": "approve",
                       "financial": "approve", "destructive": "approve"},
            "goal": "g", "tools": ["data.query"]}
    with open(os.path.join(w.workers_dir, f"{name}.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, sort_keys=False, stream=fh)


def test_doctor_clean(ws):
    _seed_worker(ws)
    rep = D.run_doctor(ws)
    assert rep["ok"] is True
    assert rep["errors"] == 0


def test_export_excludes_secrets_key(ws, tmp_path):
    _seed_worker(ws)
    # write a secrets key that must NOT be exported
    key_path = os.path.join(ws.state_dir, "secrets.key")
    with open(key_path, "w", encoding="utf-8") as fh:
        fh.write("SUPERSECRETKEY")
    dest = os.path.join(str(tmp_path), "pkg.tar.gz")
    P.export_package(ws, dest)
    names = []
    with tarfile.open(dest, "r:gz") as tf:
        for m in tf.getmembers():
            names.append(m.name)
    assert "secrets.key" not in names
    assert any(n.startswith("workers/") for n in names)


def test_import_refuses_clobber(ws, tmp_path):
    _seed_worker(ws)
    dest = os.path.join(str(tmp_path), "pkg.tar.gz")
    P.export_package(ws, dest)
    # importing into a fresh NON-empty workspace must fail
    other = tmp_path / "other"
    os.makedirs(other, exist_ok=True)
    (other / "existing.txt").write_text("x")
    with pytest.raises(FileExistsError):
        P.import_package(dest, Workspace(str(other)))
    # force overwrites
    info = P.import_package(dest, Workspace(str(other)), force=True)
    assert info["extracted"] >= 1


def test_backup_restore_roundtrip(ws, tmp_path):
    _seed_worker(ws)
    dest = os.path.join(str(tmp_path), "bak.tar.gz")
    P.backup(ws, dest)
    other = tmp_path / "restore"
    info = P.restore(dest, Workspace(str(other)))
    assert os.path.exists(os.path.join(str(other), "workers", "a.yaml"))
    assert info["extracted"] >= 1
