"""§25 worker templates / marketplace.

Fail-closed guarantees:
  * unknown template -> TemplateError
  * create refuses to clobber an existing worker (FileExistsError)
  * rendered worker is valid YAML and loadable
  * marketplace publish/import round-trips
"""

import os

import pytest

from sworker.config import Workspace, default_workspace
from sworker import templates as T


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


def test_list_templates_nonempty(ws):
    assert "analyst" in T.list_templates()


def test_unknown_template_fails(ws):
    with pytest.raises(T.TemplateError):
        T.render_template("bogus", "x", "goal")


def test_create_worker_from_template(ws):
    w = T.create_worker(ws, "analyst", "quarterly", "Q3 report")
    assert w.name == "quarterly"
    assert os.path.exists(w.path)
    assert w.role == "analyst"


def test_create_refuses_clobber(ws):
    T.create_worker(ws, "analyst", "dup", "g")
    with pytest.raises(FileExistsError):
        T.create_worker(ws, "analyst", "dup", "g")
    T.create_worker(ws, "analyst", "dup", "g", force=True)


def test_marketplace_publish_import(ws):
    T.create_worker(ws, "analyst", "reportbot", "make reports")
    T.publish_to_marketplace(ws, "reportbot")
    items = T.list_marketplace(ws)
    assert "reportbot" in items
    # remove the live worker, then import from marketplace
    os.remove(os.path.join(ws.workers_dir, "reportbot.yaml"))
    w = T.import_from_marketplace(ws, "reportbot")
    assert w.name == "reportbot"
    assert os.path.exists(w.path)


def test_marketplace_publish_duplicate_fails(ws):
    T.create_worker(ws, "analyst", "reportbot", "make reports")
    T.publish_to_marketplace(ws, "reportbot")
    with pytest.raises(FileExistsError):
        T.publish_to_marketplace(ws, "reportbot")
