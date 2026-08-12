"""§71 — coverage for the thin sales CLI verbs (lead/outreach/followups) and the
``sales_followup`` worker identity.

These are thin operators over the same modules the worker procedures call, so
the tests assert (a) the verbs reach the repository and return real data, and
(b) the new follow-up worker obeys separation-of-duties — it cannot draft,
approve, or send (egress tools are absent from its allowlist). That is a
fail-closed property of the worker boundary, not of the CLI.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from sworker.config import load_worker, Workspace
from sworker.store import WorkerStore
from sworker.sales.repository import SalesRepository, default_ledger_path

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "sworker", "sales", "templates")
DAILYSALESOS = os.path.expanduser("~/Documents/Projects/salesworkflow")


def _workspace_with_ledger() -> Path:
    home = Path(tempfile.mkdtemp())
    (home / "company").mkdir(parents=True)
    ledger_dir = home / "company" / "Experiment_Ledger"
    ledger_dir.mkdir(parents=True)
    os.environ["DAILYSALESOS_LEDGER"] = str(ledger_dir / "experiments.db")
    os.environ["DAILYSALESOS_ROOT"] = DAILYSALESOS
    os.environ["SWORKER_HOME"] = str(home)
    return home


def _init_sales(home: Path) -> None:
    """Copy worker templates + compile ICP + seed + discover so the CLI verbs
    have a real ledger with leads to operate on."""
    from sworker.sales import cli as sales_cli
    import io, contextlib

    with contextlib.redirect_stdout(io.StringIO()):
        sales_cli.cmd_init(_Args(force=True))
        sales_cli.cmd_seed(_Args(csv_name="candidates.csv"))
    # discover the seeded candidates into leads (same path a daily-run uses)
    repo = SalesRepository(default_ledger_path())
    try:
        from sworker.sales import discovery as D
        from sworker.sales.evidence import SalesEvidence
        from sworker.config import default_workspace

        acc = SalesEvidence(repo)
        cpath = os.path.join(default_workspace().company_dir, "candidates.csv")
        cands, ref = D.read_candidates(cpath)
        D.discover(repo, cands, source_ref=ref, source="candidates.csv", limit=0,
                   run_id="test", evidence=acc)
    finally:
        repo.close()


class _Args:
    def __init__(self, **kw):
        self.force = kw.get("force", False)
        self.recompile = kw.get("recompile", False)
        self.stage = ""
        self.summary = False
        self.day = ""
        self.markdown = False
        self.source = "candidates.csv"
        self.limit = 3
        self.csv_name = "candidates.csv"


def _run(capsys, *argv):
    from sworker.cli import main

    try:
        code = main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------- #
# thin CLI verbs reach the ledger and return real data
# --------------------------------------------------------------------------- #
def test_lead_pipeline_and_show(capsys):
    home = _workspace_with_ledger()
    _init_sales(home)
    code, out = _run(capsys, "sales", "pipeline")
    assert code == 0
    rows = json.loads(out)
    assert isinstance(rows, list)
    # every row carries the join fields the HTML lead list relies on
    if rows:
        assert "company_name" in rows[0]
        assert "stage" in rows[0]
        lid = rows[0]["id"]
        code, out = _run(capsys, "sales", "lead", "show", lid)
        assert code == 0
        lead = json.loads(out)
        assert lead["id"] == lid
        # the lead detail aggregates evidence/qualifications/drafts
        assert "evidence" in lead and "qualifications" in lead and "drafts" in lead


def test_lead_qualify_and_followups_due(capsys):
    home = _workspace_with_ledger()
    _init_sales(home)
    repo = SalesRepository(default_ledger_path())
    leads = repo.search_leads(limit=1)
    repo.close()
    assert leads, "seeded ledger should have at least one lead"
    lid = leads[0]["id"]
    code, out = _run(capsys, "sales", "lead", "qualify", lid)
    assert code == 0
    assert "score" in out
    code, out = _run(capsys, "sales", "followups", "due")
    assert code == 0
    data = json.loads(out)
    assert "counts" in data


def test_outreach_draft_is_gated_and_approve_rejects_bad_id(capsys):
    home = _workspace_with_ledger()
    _init_sales(home)
    repo = SalesRepository(default_ledger_path())
    leads = repo.search_leads(limit=1)
    repo.close()
    lid = leads[0]["id"]
    # draft is gated: the CLI must report it still requires approval (not auto-sent)
    code, out = _run(capsys, "sales", "outreach", "draft", lid)
    assert code == 0
    assert "requires_approval=True" in out, out
    assert "draft_id=" in out
    # approving a non-existent draft must fail closed (rc != 0), not fabricate an approval
    code, _ = _run(capsys, "sales", "outreach", "approve", "no-such-draft", "--approved-by", "op")
    assert code != 0


# --------------------------------------------------------------------------- #
# sales_followup worker — separation of duties (fail-closed allowlist)
# --------------------------------------------------------------------------- #
def test_sales_followup_worker_is_readonly_on_egress():
    path = os.path.join(TEMPLATES, "sales_followup.yaml")
    w = load_worker(path, Workspace(os.path.expanduser("~")))
    # A follow-up worker may schedule + read, but MUST NOT hold egress tools.
    egress = {"sales_draft_outreach", "sales_approve_draft", "sales_record_sent", "sales_bulk_send"}
    held = egress & set(w.tools)
    assert not held, f"follow-up worker must not hold egress tools: {held}"
    # it should be able to do its job
    assert "sales_schedule_followup" in w.tools
    assert "sales_followup_due" in w.tools


def test_sales_followup_worker_loads_under_engine_contract():
    """The new worker is still a normal WorkerConfig; the engine must load it
    without a bespoke branch (same contract test philosophy as §37)."""
    path = os.path.join(TEMPLATES, "sales_followup.yaml")
    ws = Workspace(str(_workspace_with_ledger()))
    ws.ensure()
    w = load_worker(path, ws)
    assert w.name == "sales_followup"
    # the fs boundary + policy are present and legal
    assert any(r.endswith("company") for r in w.fs_roots), w.fs_roots
    assert set(w.policy.keys()) == {"read", "reversible", "external", "financial", "destructive"}
