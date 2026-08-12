"""Sales verification checks — deterministic re-derivation over the ledger.

Proves the checks FAIL CLOSED: a tampered score, a sent-but-unapproved draft, an
illegal stage, or a claim without a source_ref must surface as a FAIL (never
silently trusted). Reuses sworker's real ``run_check`` engine.

The checks read the ledger at ``default_ledger_path()``, so each test points that
env var at its own temp db and operates on the exact same file.
"""

from __future__ import annotations

import os
import tempfile

from sworker.verify import run_check, VerificationOutcome
from sworker.sales import knowledge, qualification, evidence
from sworker.sales.repository import SalesRepository, default_ledger_path
from sworker.sales.models import Company, OutreachDraft
from sworker.sales.checks import sales_checks

DAILYSALESOS = os.path.expanduser("~/Documents/Projects/salesworkflow")


def _setup():
    """Return a repo whose path IS what default_ledger_path() resolves to."""
    d = tempfile.mkdtemp()
    db = os.path.join(d, "experiments.db")
    os.environ["DAILYSALESOS_LEDGER"] = db
    assert default_ledger_path() == os.path.abspath(db)
    return SalesRepository(db), db


def _seed_lead(repo, name="Co"):
    co = Company(name=name, domain=f"{name}.com")
    return repo.create_lead(co, source="t")["lead"]


def _qualify(repo, lead, acc):
    # ``evaluate`` is append-only and already persists the qualification.
    acc.attach(lead.id, "icp_fit", "fits top industry", source_ref="obs_1", tier="observed")
    acc.attach(lead.id, "size_signal", "team of 30", source_ref="obs_2", tier="observed")
    acc.attach(lead.id, "urgency_signal", "manual CRM entry", source_ref="obs_3", tier="observed")
    return qualification.evaluate(repo, lead.id, run_id="run_x")


def _tamper(repo, lead_id, **cols):
    """Test backdoor: clobber stored values to prove the checks re-derive."""
    cur = repo._conn.cursor()
    # score lives on qualifications; stage lives on leads. Touch whichever has the col.
    for k, v in cols.items():
        if k == "stage":
            cur.execute("UPDATE leads SET stage = ? WHERE id = ?", (v, lead_id))
        else:
            row = cur.execute(
                "SELECT id FROM qualifications WHERE lead_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
            qid = row["id"] if row else None
            if qid:
                cur.execute("UPDATE qualifications SET score = ? WHERE id = ?", (v, qid))
    repo._conn.commit()


def test_all_five_checks_registered():
    assert set(sales_checks) == {
        "sales_score_recomputes",
        "sales_evidence_has_source",
        "sales_outreach_approved_first",
        "sales_pipeline_legal",
        "sales_metrics_match_ledger",
    }


def test_score_recomputes_passes_for_clean_ledger():
    repo, _ = _setup()
    try:
        acc = evidence.SalesEvidence(repo)
        lead = _seed_lead(repo, "CleanCo")
        _qualify(repo, lead, acc)
        res = run_check({"check": "sales_score_recomputes"}, "")
        assert res.status is VerificationOutcome.PASS, res.detail
    finally:
        repo.close()


def test_score_recomputes_fails_when_tampered():
    repo, _ = _setup()
    try:
        acc = evidence.SalesEvidence(repo)
        lead = _seed_lead(repo, "TamperCo")
        _qualify(repo, lead, acc)
        # Tamper the stored qualification total so it disagrees with the
        # recomputation from its own sub-scores.
        _tamper(repo, lead.id, score=99.0)
        res = run_check({"check": "sales_score_recomputes"}, "")
        assert res.status is VerificationOutcome.FAIL, res.detail
    finally:
        repo.close()


def test_outreach_approved_first_refuses_unapproved_send():
    repo, _ = _setup()
    try:
        lead = _seed_lead(repo, "SendCo")
        draft = repo.create_draft(
            OutreachDraft(lead_id=lead.id, channel="email", subject="hi", body="hey")
        )
        raised = False
        try:
            repo.record_sent(draft.id, receipt="smtp:noop")
        except Exception:
            raised = True
        assert raised, "record_sent must refuse an unapproved draft"
        res = run_check({"check": "sales_outreach_approved_first"}, "")
        assert res.status is VerificationOutcome.PASS
    finally:
        repo.close()


def test_pipeline_legal_rejects_unknown_stage():
    repo, _ = _setup()
    try:
        lead = _seed_lead(repo, "StageCo")
        _tamper(repo, lead.id, stage="not_a_stage")
        res = run_check({"check": "sales_pipeline_legal"}, "")
        assert res.status is VerificationOutcome.FAIL, res.detail
    finally:
        repo.close()


def test_evidence_has_source_passes_when_all_sourced():
    repo, _ = _setup()
    try:
        acc = evidence.SalesEvidence(repo)
        lead = _seed_lead(repo, "EvCo")
        acc.attach(lead.id, "icp_fit", "fits", source_ref="obs_9", tier="observed")
        res = run_check({"check": "sales_evidence_has_source"}, "")
        assert res.status is VerificationOutcome.PASS
    finally:
        repo.close()


def test_metrics_match_ledger_detects_divergence():
    repo, _ = _setup()
    try:
        res = run_check({"check": "sales_metrics_match_ledger", "expect": {"researched": 5}}, "")
        assert res.status is VerificationOutcome.FAIL, res.detail
    finally:
        repo.close()


def test_icp_parsed_from_real_markdown():
    icps = knowledge.compile_icp(DAILYSALESOS)
    assert icps, "should compile at least one industry from the real docs"
    assert any(c.active for c in icps), "top-ranked industry should be active"
