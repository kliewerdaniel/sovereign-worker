"""Tests for the company-knowledge bridge (sworker.knowledge + tools.knowledge).

Covers both retrieval paths without a network or a real Atlas install:

  * BLACK path  -> Atlas not importable -> deterministic grep over company/*.md,
    clearly labelled "[degraded: raw document grep, knowledge not compiled]".
  * COMPILED path -> a minimal fake `hermes_atlas` package is injected on
    sys.path so the compiled-claim retrieval branch runs end to end.

Run with:  env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/
"""

from __future__ import annotations

import os
import sys
import json
import time
import textwrap
from pathlib import Path

import pytest

from sworker.config import Workspace
from sworker.tools.base import ToolContext
from sworker.tools.knowledge import KnowledgeSearch, KnowledgeExplain


MD_NOTE = textwrap.dedent(
    """\
    # Acme Coffee — Pricing Policy

    We grant a 10% volume discount to partners ordering above 500 units.
    Free shipping applies only to repeat customers.
    """
)


@pytest.fixture()
def ws(tmp_path):
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "pricing.md").write_text(MD_NOTE)
    (home / "workers").mkdir(parents=True)
    w = Workspace(str(home))
    w.ensure()
    return w


def _ctx(ws: Workspace) -> ToolContext:
    return ToolContext(
        worker="acme-analyst",
        run_id="run_test",
        workspace=str(ws.root),
        fs_roots=[str(Path(ws.root) / "company")],
        artifacts_dir=str(Path(ws.root) / "artifacts"),
    )


def test_black_path_degraded_grep(ws):
    """No Atlas importable -> labelled grep, never silent fabrication."""
    # Ensure Atlas is not resolvable for this test regardless of checkout layout.
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    import importlib

    import sworker.knowledge as K

    importlib.reload(K)  # reset cached ATLAS_AVAILABLE
    assert K.ATLAS_AVAILABLE is False, "expected Atlas unavailable in this env"

    ctx = _ctx(ws)
    res = KnowledgeSearch().run(ctx, {"query": "volume discount"})
    assert res.ok
    assert res.data["mode"] == "grep"
    assert "[degraded: raw document grep, knowledge not compiled]" in res.output
    assert "10% volume discount" in res.output
    # grep evidence points at the real file:line, not a fabricated claim id
    assert res.evidence and res.evidence[0]["source_ref"].endswith(":1") or any(
        "pricing.md" in e["source_ref"] for e in res.evidence
    )


def test_compiled_path_returns_claims(tmp_path):
    """A minimal fake hermes_atlas yields compiled-claim retrieval.

    We drop a fake package on sys.path and point SWORKER_ATLAS_HOME at it so
    knowledge._try_import picks it up. The fake exposes just enough of the
    Atlas store API for search_claims to run deterministically.
    """
    atlas_home = tmp_path / "hermes-atlas"
    (atlas_home / "hermes_atlas").mkdir(parents=True)
    (atlas_home / "hermes_atlas" / "__init__.py").write_text("__version__ = '0.0.0'\n")
    (atlas_home / "hermes_atlas" / "store.py").write_text(
        textwrap.dedent(
            """\
            class AtlasStore:
                def __init__(self, path): pass
                def read_all(self, table):
                    if table == "claims":
                        return [{"id": "c1", "text": "acme grants a volume discount",
                                 "confidence": 0.9, "source_ids": ["s1"]}]
                    if table == "sources":
                        return [{"id": "s1", "title": "Pricing Policy",
                                 "path": "company/pricing.md"}]
                    return []
            """
        )
    )
    # The real Atlas checkout may already be imported/cached this session; force
    # the fake onto sys.path AND evict any cached hermes_atlas so `import
    # hermes_atlas` resolves to our stub instead of the sibling project.
    fake_pkg = str(atlas_home)
    sys.path.insert(0, fake_pkg)
    for m in [k for k in sys.modules if k == "hermes_atlas" or k.startswith("hermes_atlas.")]:
        sys.modules.pop(m, None)
    os.environ["SWORKER_ATLAS_HOME"] = str(atlas_home)
    # Force re-import of the knowledge module so it discovers the fake Atlas.
    import importlib

    import sworker.knowledge as K

    importlib.reload(K)
    assert K.atlas_status()["available"] is True

    company = tmp_path / "company"
    company.mkdir(parents=True)
    (company / "pricing.md").write_text(MD_NOTE)

    # Build a workspace free of .state/atlas so search hits the compiled branch.
    ws_home = tmp_path / "acme"
    ws_home.mkdir()
    (ws_home / "company").mkdir(parents=True)
    (ws_home / "company" / "pricing.md").write_text(MD_NOTE)
    # search_claims only reads compiled claims when the compiled store exists.
    (ws_home / ".state" / "atlas").mkdir(parents=True)
    from sworker.config import Workspace

    ws = Workspace(str(ws_home))
    ws.ensure()
    ctx = _ctx(ws)

    res = KnowledgeSearch().run(ctx, {"query": "volume discount"})
    assert res.ok
    assert res.data["mode"] == "compiled", res.data
    assert res.data["count"] >= 1
    assert "acme grants a volume discount" in res.output
    # compiled evidence references the real claim id, not grep coordinates
    assert res.evidence and res.evidence[0]["source_ref"] == "c1"
    assert res.evidence[0].get("atlas_claim") is True

    # cleanup: drop the env override + fake path so they don't leak into other tests
    os.environ.pop("SWORKER_ATLAS_HOME", None)
    if fake_pkg in sys.path:
        sys.path.remove(fake_pkg)
    for m in [k for k in sys.modules if k == "hermes_atlas" or k.startswith("hermes_atlas.")]:
        sys.modules.pop(m, None)
    importlib.reload(K)


def test_knowledge_explain_handles_missing_claim(ws):
    """Explaining an unknown claim returns a clean failure, not an exception."""
    ctx = _ctx(ws)
    res = KnowledgeExplain().run(ctx, {"claim_id": "does-not-exist"})
    assert res.ok is False
    assert "does-not-exist" in res.error


# ---------------------------------------------------------------------------
# §17 Atlas deepening — status / stale / rebuild / incremental (real Atlas)
# ---------------------------------------------------------------------------
# These run against the real hermes_atlas checkout, which is importable in this
# dev environment. If Atlas is ever unavailable they skip rather than fake a pass.


@pytest.fixture()
def atlas_ws(tmp_path):
    """A real workspace with a company corpus and a compiled Atlas index."""
    import sworker.knowledge as K

    if not K.atlas_status()["available"]:
        pytest.skip("hermes_atlas not importable; §17 live tests need it")
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "workers").mkdir(parents=True)
    src = home / "company" / "pricing.md"
    src.write_text(
        "# Acme Pricing Policy\n\n"
        "We grant a 10% volume discount to partners ordering above 500 units.\n"
        "Free shipping applies only to repeat customers.\n"
    )
    w = Workspace(str(home))
    w.ensure()
    rep = K.incremental_compile([str(w.company_dir)], w.atlas_dir)
    assert rep.get("ok"), rep
    return w


def test_atlas_status_current_after_compile(atlas_ws):
    from sworker import knowledge as K

    st = K.atlas_index_status(atlas_ws.atlas_dir)
    assert st["compiled"] is True
    assert st["sources"] >= 1
    assert st["fingerprint"]
    assert st["stale_count"] == 0
    assert st["missing_count"] == 0


def test_atlas_status_detects_stale_edit(atlas_ws):
    """Editing a source after compile is detected as STALE (checksum mismatch)."""
    from sworker import knowledge as K

    src = os.path.join(atlas_ws.company_dir, "pricing.md")
    open(src, "w").write(
        "# Acme Pricing Policy\n\n"
        "We grant a 20% volume discount to partners ordering above 250 units.\n"
    )
    st = K.atlas_index_status(atlas_ws.atlas_dir)
    assert st["stale_count"] == 1, st
    s = st["stale"][0]
    assert s["path"].endswith("pricing.md")
    assert s["recorded_checksum"] != s["live_checksum"]


def test_atlas_status_detects_missing_source(atlas_ws):
    """Deleting a source after compile is detected as MISSING."""
    from sworker import knowledge as K

    src = os.path.join(atlas_ws.company_dir, "pricing.md")
    os.unlink(src)
    st = K.atlas_index_status(atlas_ws.atlas_dir)
    assert st["missing_count"] == 1, st
    assert st["missing_sources"][0]["path"].endswith("pricing.md")


def test_incremental_compile_is_idempotent(atlas_ws):
    """Compiling again with no source change adds no changelog entries."""
    from sworker import knowledge as K

    before = K.atlas_index_status(atlas_ws.atlas_dir)["changelog_entries"]
    rep = K.incremental_compile([str(atlas_ws.company_dir)], atlas_ws.atlas_dir)
    assert rep.get("ok")
    after = K.atlas_index_status(atlas_ws.atlas_dir)["changelog_entries"]
    # No source changed -> the delta path writes nothing.
    assert after == before, (before, after)


def test_rebuild_wipes_and_recompiles(atlas_ws):
    """rebuild_index wipes the store and recompiles from scratch."""
    from sworker import knowledge as K

    rep = K.rebuild_index([str(atlas_ws.company_dir)], atlas_ws.atlas_dir)
    assert rep.get("ok"), rep
    st = K.atlas_index_status(atlas_ws.atlas_dir)
    assert st["compiled"] is True
    assert st["sources"] >= 1
    # A fresh rebuild produces a clean changelog (entries from this cycle only).
    assert st["changelog_entries"] >= 1
    assert st["stale_count"] == 0


def test_atlas_status_fail_closed_when_no_index(tmp_path):
    """A non-existent index reports NOT compiled; never a fabricated status."""
    from sworker import knowledge as K

    empty = tmp_path / "nope"
    st = K.atlas_index_status(str(empty))
    assert st["compiled"] is False
    assert st["reason"] == "no-index"
    assert st["stale_count"] == 0
    assert st["sources"] == 0


# ---------------------------------------------------------------------------
# §18 ingestion adapters — pdf/docx/json (fail-closed)
# ---------------------------------------------------------------------------


def test_extract_json_normalizes_to_markdown(tmp_path):
    from sworker import knowledge as K

    p = tmp_path / "data.json"
    p.write_text(json.dumps(
        {"company": "Acme", "regions": [{"name": "north", "rev": 120}, {"name": "south", "rev": 90}]}
    ))
    text = K.extract_json(str(p))
    assert text is not None
    assert "## company" in text
    assert "Acme" in text
    assert "| name | rev |" in text  # array-of-objects -> table
    assert "| north | 120 |" in text


def test_collect_sources_picks_up_json_and_skips_unsupported(tmp_path):
    from sworker import knowledge as K

    (tmp_path / "a.md").write_text("# A\nAcme runs north and south.\n")
    (tmp_path / "b.json").write_text(json.dumps(
        {"company": "Acme Coffee", "note": "Acme operates in north and south regions."}
    ))
    (tmp_path / "c.txt").write_text("ignored plaintext")  # unsupported ext
    # a pdf with no parser available -> reported as skipped, not ingested
    (tmp_path / "d.pdf").write_bytes(b"%PDF-1.4 fake")
    found = K.collect_sources([str(tmp_path)])
    assert len(found["markdown"]) == 1
    assert len(found["other"]) == 1
    assert found["other"][0][1] == ".json"
    skipped = {os.path.basename(s[0]): s[2] for s in found["skipped"]}
    assert "d.pdf" in skipped
    assert skipped["d.pdf"] == "pdf-unavailable"


def test_compile_knowledge_ingests_json_source_via_adapter(tmp_path):
    """A JSON source flows through the adapter into Atlas with the real path."""
    import shutil
    from sworker.config import Workspace
    from sworker import knowledge as K

    if not K.atlas_status()["available"]:
        pytest.skip("hermes_atlas not importable")
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "notes.md").write_text("# Acme\nAcme operates in north and south regions.\n")
    (home / "company" / "data.json").write_text(
        json.dumps({"company": "Acme", "regions": [{"name": "north", "rev": 120}]})
    )
    ws = Workspace(str(home))
    ws.ensure()
    rep = K.compile_knowledge([ws.company_dir], ws.atlas_dir)
    assert rep.get("ok"), rep
    assert rep["adapter_files"] == 1, rep
    assert rep["markdown_files"] == 1, rep
    from hermes_atlas.store import AtlasStore

    st = AtlasStore(ws.atlas_dir)
    jsrc = [s for s in st.read_all("sources") if s.get("path", "").endswith(".json")]
    assert jsrc, "json source not recorded in store"
    assert jsrc[0]["path"].endswith(".json")
    # §17 stale detection now tracks the real json file
    stale = K.atlas_index_status(ws.atlas_dir)
    assert stale["sources"] == 2
    shutil.rmtree(home)


def test_collect_sources_no_markdown_yields_reports_adapters(tmp_path):
    """With no ingestable content, the fail-closed report still lists adapters."""
    from sworker import knowledge as K

    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4 fake")
    found = K.collect_sources([str(tmp_path)])
    assert found["count"] == 0
    assert any(s[2] == "pdf-unavailable" for s in found["skipped"])


# ---------------------------------------------------------------------------
# §19 sync watcher — fail-closed poll / recompile on change
# ---------------------------------------------------------------------------


def test_watch_knowledge_recompiles_on_file_change(tmp_path):
    """Editing a source triggers an incremental recompile via the watcher."""
    import shutil
    from sworker.config import Workspace
    from sworker import knowledge as K

    if not K.atlas_status()["available"]:
        pytest.skip("hermes_atlas not importable")
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "notes.md").write_text("# Acme\nAcme operates in north and south regions.\n")
    ws = Workspace(str(home))
    ws.ensure()
    # initial compile
    K.compile_knowledge([ws.company_dir], ws.atlas_dir)
    from hermes_atlas.store import AtlasStore

    before = AtlasStore(ws.atlas_dir).stats().get("sources", 0)
    events: list = []
    stop = K.watch_knowledge(
        [ws.company_dir], ws.atlas_dir, interval=0.2,
        on_compile=lambda r: events.append(r),
    )
    try:
        # give the loop one poll to record the baseline snapshot
        time.sleep(0.3)
        # now change a source file
        (home / "company" / "notes.md").write_text(
            "# Acme\nAcme operates in north and south regions. Q3 revenue rose 12%.\n"
        )
        # wait for the watcher to detect + recompile
        for _ in range(50):
            if events:
                break
            time.sleep(0.1)
    finally:
        stop.set()
    shutil.rmtree(home)
    assert events, "watcher never recompiled after a file change"
    assert events[-1].get("ok"), events[-1]


def test_watch_knowledge_fail_closed_on_compile_error(tmp_path, monkeypatch):
    """A compile error is reported, not swallowed; the watcher keeps running."""
    import shutil
    from sworker.config import Workspace
    from sworker import knowledge as K

    if not K.atlas_status()["available"]:
        pytest.skip("hermes_atlas not importable")
    home = tmp_path / "acme"
    (home / "company").mkdir(parents=True)
    (home / "company" / "notes.md").write_text("# Acme\nAcme runs north and south.\n")
    ws = Workspace(str(home))
    ws.ensure()
    K.compile_knowledge([ws.company_dir], ws.atlas_dir)

    def _boom(*a, **k):
        raise RuntimeError("injected compile failure")

    events: list = []
    stop = K.watch_knowledge(
        [ws.company_dir], ws.atlas_dir, interval=0.2,
        on_compile=lambda r: events.append(r),
    )
    try:
        time.sleep(0.3)
        # force every incremental compile to fail; watcher must still report it
        monkeypatch.setattr(K, "incremental_compile", _boom)
        (home / "company" / "notes.md").write_text("# Acme\nAcme runs north, south, east.\n")
        for _ in range(50):
            if any(e.get("reason") == "watch-compile-error" for e in events):
                break
            time.sleep(0.1)
    finally:
        stop.set()
    shutil.rmtree(home)
    assert any(e.get("reason") == "watch-compile-error" for e in events), events


def test_roots_snapshot_detects_change(tmp_path):
    """_snapshot_changed is fail-closed and sensitive to add/edit/remove."""
    from sworker import knowledge as K

    a = tmp_path / "a.md"
    a.write_text("hello")
    snap1 = K._roots_snapshot([str(tmp_path)])
    assert a.resolve().as_posix() in {os.path.abspath(k) for k in snap1}
    assert not K._snapshot_changed(snap1, snap1)
    # edit -> size/mtime change
    time.sleep(0.01)
    a.write_text("hello world longer")
    snap2 = K._roots_snapshot([str(tmp_path)])
    assert K._snapshot_changed(snap1, snap2)
    # add a file
    b = tmp_path / "b.md"
    b.write_text("new")
    snap3 = K._roots_snapshot([str(tmp_path)])
    assert K._snapshot_changed(snap2, snap3)
    # remove a file
    b.unlink()
    snap4 = K._roots_snapshot([str(tmp_path)])
    assert K._snapshot_changed(snap3, snap4)
