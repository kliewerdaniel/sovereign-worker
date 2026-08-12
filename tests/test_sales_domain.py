"""Sales domain invariants — pipeline legality, qualification determinism,
dedupe, and the send gate. Pure repository/domain logic, no model, no network.
"""

from __future__ import annotations

import os
import tempfile

from sworker.sales import qualification, evidence
from sworker.sales.repository import SalesRepository, SalesError
from sworker.sales.models import Company, OutreachDraft
from sworker.sales.pipeline import STAGES, can_move


def _repo():
    d = tempfile.mkdtemp()
    return SalesRepository(os.path.join(d, "experiments.db"))


def _seed(repo, name="Co"):
    co = Company(name=name, domain=f"{name}.com")
    return repo.create_lead(co, source="t")["lead"]


def test_pipeline_stage_count_is_15_documents_14_intent():
    # CRM_Pipeline.md documents 14 stages; "Won/Lost" splits into terminal enums.
    assert len(STAGES) == 15


def test_can_move_legal_transitions():
    assert can_move("prospect", "contacted")
    assert can_move("contacted", "responded")
    assert can_move("responded", "discovery_scheduled")
    assert can_move("negotiation", "won")  # terminal transition allowed


def test_move_stage_refuses_illegal_jump():
    repo = _repo()
    try:
        lead = _seed(repo, "JumpCo")
        try:
            repo.move_stage(lead.id, "won")
            assert False, "illegal jump should be refused"
        except SalesError:
            pass
        mv = repo.move_stage(lead.id, "contacted", reason="test", run_id="r1")
        assert mv["to"] == "contacted"
    finally:
        repo.close()


def test_dedupe_suppresses_duplicate_company():
    repo = _repo()
    try:
        a = repo.create_lead(Company(name="Acme Realty", domain="acme.com"), source="csv")
        b = repo.create_lead(Company(name="Acme Realty", domain="acme.com"), source="csv")
        assert a["created"] is True
        assert b["created"] is False
        assert a["lead"].id == b["lead"].id
    finally:
        repo.close()


def test_qualification_is_deterministic_and_scored():
    repo = _repo()
    try:
        acc = evidence.SalesEvidence(repo)
        lead = _seed(repo, "QualCo")
        acc.attach(lead.id, "icp_fit", "fits top industry", source_ref="obs_1", tier="observed")
        acc.attach(lead.id, "size_signal", "team of 30", source_ref="obs_2", tier="observed")
        acc.attach(lead.id, "urgency_signal", "manual CRM entry", source_ref="obs_3", tier="observed")
        q1 = qualification.evaluate(repo, lead.id, run_id="r1")
        q2 = qualification.evaluate(repo, lead.id, run_id="r2")
        assert q1.score == q2.score
        assert 0 <= q1.score <= 100
        br = qualification.score_breakdown(repo, lead.id)
        assert br["matches"], br
    finally:
        repo.close()


def test_send_gate_refuses_unapproved_draft():
    repo = _repo()
    try:
        lead = _seed(repo, "GateCo")
        draft = repo.create_draft(
            OutreachDraft(lead_id=lead.id, channel="email", subject="s", body="b")
        )
        assert draft.state.value == "draft"
        raised = False
        try:
            repo.record_sent(draft.id, receipt="smtp:noop")
        except SalesError:
            raised = True
        assert raised, "sending an unapproved draft must be refused"
        # After approval, it goes through.
        repo.approve_draft(draft.id, "operator")
        sent = repo.record_sent(draft.id, receipt="smtp:noop")
        assert sent["state"] == "sent"
    finally:
        repo.close()


def test_record_sent_feeds_experiment_only_when_real():
    """Design: never fabricate experiment/prospect rows — feed only if they exist."""
    repo = _repo()
    try:
        lead = _seed(repo, "ExpCo")
        draft = repo.create_draft(
            OutreachDraft(lead_id=lead.id, channel="email", subject="s", body="b")
        )
        repo.approve_draft(draft.id, "operator")
        # A non-existent experiment_id must NOT raise and must NOT insert a row.
        sent = repo.record_sent(draft.id, receipt="smtp:noop", experiment_id="EXP-NOPE")
        assert sent["state"] == "sent"
        touched = repo.raw(
            "SELECT COUNT(*) AS n FROM outreach_touches WHERE experiment_id = 'EXP-NOPE'"
        )
        assert touched[0]["n"] == 0, "must not fabricate an outreach_touch for a bogus experiment"
    finally:
        repo.close()
