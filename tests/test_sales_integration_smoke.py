"""End-to-end smoke test of the sales boundary layer.

Runs the full local-first loop against a temp Experiment_Ledger and the real
DailySalesOS markdown (for ICP/offer/sequence parsing), then re-verifies with the
sales checks. No network, no model: this proves the deterministic path works.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from sworker.sales import discovery, evidence, followup, knowledge, metrics, outreach, qualification, research
from sworker.sales.checks import sales_checks
from sworker.sales.repository import SalesRepository, SalesError, default_ledger_path
from sworker.sales.tools.base import SALES_TOOLS
from sworker.verify import run_check


DAILYSALESOS = os.path.expanduser("~/Documents/Projects/salesworkflow")


def _write_candidates(tmp: Path) -> Path:
    p = tmp / "candidates.csv"
    p.write_text(
        "company,domain,industry,geography,agents,contact,broker_email\n"
        "Acme Realty,acme.com,Real Estate,Austin,12,Jane,Jane@acme.com\n"
        "Beta Law,betalaw.com,Small Law Firms,Boston,40,Lou,lou@betalaw.com\n"
        "Acme Realty,acme.com,Real Estate,Austin,12,Jane,Jane@acme.com\n",
        encoding="utf-8",
    )
    return p


def _write_source(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def test_full_loop():
    tmp = Path(tempfile.mkdtemp())
    # Put the ledger under the workspace/company boundary like a real worker would.
    workspace = tmp / "ws"
    (workspace / "company").mkdir(parents=True)
    ledger = workspace / "company" / "Experiment_Ledger" / "experiments.db"
    repo = SalesRepository(str(ledger))

    # Compile ICP from the real markdown (offline: uses the files on disk).
    icps = knowledge.compile_icp(DAILYSALESOS)
    for icp in icps:
        repo.upsert_icp(icp)
    assert any(c.active for c in icps), "top ICP should be active"

    acc = evidence.SalesEvidence(repo)

    # 1) DISCOVER
    cand_path = _write_candidates(tmp)
    rows, sref = discovery.read_candidates(str(cand_path))
    result = discovery.discover(
        repo,
        rows,
        source_ref=sref,
        evidence=acc,
    )
    assert result["created_count"] == 2, result
    assert result["duplicate_count"] == 1, result  # the repeated Acme row

    lead_ids = [r["lead_id"] for r in result["created"]]

    # 2) RESEARCH from a permitted source file (signal phrases + pain phrases).
    src = _write_source(
        tmp,
        "acme_note.md",
        "Acme Realty has 12 agents and we have to copy them over by hand into our CRM.\n"
        "Sometimes leads fall through the cracks because no one follows up on weekends.\n"
        "We don't track how many leads actually convert. jane@acme.com\n",
    )
    res = research.research_lead(repo, lead_ids[0], [str(src)], evidence=acc, run_id="run_x")
    assert res["evidence_count"] >= 3, res
    assert res["pain_points"], res
    assert not res["degraded"], res

    # 3) QUALIFY (deterministic)
    qual = qualification.evaluate(repo, lead_ids[0], run_id="run_x")
    assert 0 <= qual.score <= 100
    break2 = qualification.score_breakdown(repo, lead_ids[0])
    assert break2["matches"], break2

    # 4) DRAFT (no model)
    offer = knowledge.parse_core_offer(DAILYSALESOS)
    seqs = knowledge.parse_followup_sequences(DAILYSALESOS)
    draft = outreach.prepare(
        repo, lead_ids[0], sequences=seqs, offer=offer, run_id="run_x"
    )
    assert draft["requires_approval"] is True
    draft_id = draft["draft"]["id"]

    # 5) APPROVE then RECORD SENT (external path)
    repo.approve_draft(draft_id, "operator")
    sent = repo.record_sent(draft_id, receipt="smtp:noop", experiment_id="EXP-001")
    assert sent["state"] == "sent", sent

    # 6) MOVE STAGE
    mv = repo.move_stage(lead_ids[0], "contacted", reason="outreach sent", run_id="run_x")
    assert mv["to"] == "contacted", mv
    # illegal transition must be refused
    try:
        repo.move_stage(lead_ids[0], "won")
        assert False, "illegal jump should fail"
    except SalesError:
        pass

    # 7) FOLLOW-UP scheduling per stage rule, idempotent
    # A freshly-discovered PROSPECT has no follow-up rule (its next action is the
    # first outreach); move lead into CONTACTED like a real run would, then test.
    repo.move_stage(lead_ids[1], "contacted", reason="outreach sent", run_id="run_x")
    f1 = followup.schedule_for_lead(repo, lead_ids[1], sequences=seqs, run_id="run_x")
    assert f1["created"] is True, f1
    f2 = followup.schedule_for_lead(repo, lead_ids[1], sequences=seqs, run_id="run_x")
    assert f2["created"] is False  # already one open

    # 8) METRICS + target check from real markdown
    tgt = knowledge.parse_daily_targets(DAILYSALESOS)
    report = metrics.daily_report(repo, targets=tgt["targets"], targets_source=tgt["source_doc"])
    assert "outreach_sent" in report["counts"]
    assert report["vs_target"], "targets should have parsed from the markdown"

    # 9) VERIFICATION CHECKS (re-derive from ledger)
    for name in sales_checks:
        cr = run_check({"check": name}, workspace=str(workspace))
        assert cr.status.value == "PASS", (name, cr.detail)

    # 10) DAILY REPORT markdown renders
    md = metrics.render_markdown(report)
    assert "Daily Sales Report" in md

    repo.close()
    print("OK full loop: 2 leads, 1 qualified+contacted, draft approved+sent, checks PASS")


if __name__ == "__main__":
    test_full_loop()
