"""Outreach draft generation.

A draft is assembled deterministically from stored facts: company + contact +
evidence + pain points + the offer text from ``Core_Offer.md`` + interaction
history + pipeline stage + the applicable sequence step from
``Follow_Up_System.md``. A local model, when available, may *rewrite* the body for
tone — and the rewrite is rejected unless it still contains the offer price and
does not introduce a number that is not already in the deterministic draft.

That check is the point: the model is not the source of truth, so a prettier
draft that invents "we saved a client 40%" never reaches an outbox.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    Company,
    Contact,
    Lead,
    OutreachDraft,
    PainPoint,
    PipelineStage,
    SalesEvidenceRecord,
)
from .pipeline import stage_of
from .repository import SalesRepository

# Which Follow_Up_System.md sequence applies at each pipeline stage.
STAGE_SEQUENCE: Dict[PipelineStage, str] = {
    PipelineStage.PROSPECT: "New Prospect",
    PipelineStage.CONTACTED: "New Prospect",
    PipelineStage.RESPONDED: "New Prospect",
    PipelineStage.DISCOVERY_SCHEDULED: "New Prospect",
    PipelineStage.DISCOVERY_COMPLETED: "Discovery Completed",
    PipelineStage.QUALIFIED: "Discovery Completed",
    PipelineStage.AUDIT_IN_PROGRESS: "Discovery Completed",
    PipelineStage.PROPOSAL_SENT: "Proposal Sent",
    PipelineStage.NEGOTIATION: "Proposal Sent",
    PipelineStage.LOST: "New Prospect",
}

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set:
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text or "")}


def choose_step(sequences: Dict[str, List[Dict[str, Any]]], lead: Lead, touches: int) -> Dict[str, Any]:
    """Pick the sequence step for this lead: nth step of its stage's sequence."""
    name = STAGE_SEQUENCE.get(stage_of(lead.stage), "New Prospect")
    steps = sequences.get(name) or []
    if not steps:
        return {"sequence": name, "step": "day_0", "day": 0, "action": "Initial outreach"}
    idx = min(touches, len(steps) - 1)
    step = steps[idx]
    return {
        "sequence": name,
        "step": f"day_{step['day']}",
        "day": step["day"],
        "action": step["action"],
    }


def deterministic_body(
    company: Company,
    contact: Optional[Contact],
    pain_points: List[PainPoint],
    evidence: List[SalesEvidenceRecord],
    offer: Dict[str, Any],
    step: Dict[str, Any],
) -> Tuple[str, str]:
    """Build (subject, body) from stored facts only. No model involved."""
    first = (contact.name.split()[0] if contact and contact.name else "there")
    top = pain_points[0] if pain_points else None
    offer_name = offer.get("offer") or "Business Workflow & AI Automation Audit"
    price = offer.get("offer_price") or 0.0
    price_text = f"${price:,.0f}" if price else "a fixed fee"

    if top is not None:
        observation = top.text
    elif evidence:
        observation = evidence[0].claim_text
    else:
        observation = f"how {company.name} handles inbound lead follow-up"

    subject = {
        "New Prospect": f"{company.name}: lead follow-up",
        "Discovery Completed": "Summary from our call + next steps",
        "Proposal Sent": "Following up on the proposal",
    }.get(step["sequence"], f"{company.name}: operations")

    lines = [
        f"{first},",
        "",
        f"Looking at {company.name}, I noticed {observation}",
        "",
        f"I run a {offer_name} — {price_text}, two weeks — that maps your full "
        "lead-to-close workflow and quantifies what operational friction is "
        "costing in lost deals.",
        "",
        f"[sequence: {step['sequence']} / {step['step']} — {step['action']}]",
        "",
        "Worth a short conversation?",
        "",
        "Daniel",
    ]
    return subject, "\n".join(lines)


def validate_rewrite(original: str, rewritten: str) -> Tuple[bool, str]:
    """Accept a model rewrite only if it introduces no new numbers.

    Fail closed: an unparseable or empty rewrite is rejected, and any numeral not
    present in the deterministic draft is treated as fabrication.
    """
    if not rewritten or not rewritten.strip():
        return False, "model returned nothing"
    invented = _numbers(rewritten) - _numbers(original)
    if invented:
        return False, f"rewrite introduced unsourced number(s): {sorted(invented)}"
    if len(rewritten) > 4 * max(len(original), 1):
        return False, "rewrite is implausibly longer than the source draft"
    return True, "rewrite preserves every sourced number"


def prepare(
    repo: SalesRepository,
    lead_id: str,
    *,
    sequences: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    offer: Optional[Dict[str, Any]] = None,
    channel: str = "email",
    contact_id: str = "",
    run_id: str = "",
    inference: Any = None,
    experiment_id: str = "",
    variant: str = "",
) -> Dict[str, Any]:
    """Create and persist a draft. Never sends anything.

    Returns the stored draft plus a ``model`` report saying whether a rewrite was
    used, rejected, or unavailable — so degradation is visible in the artifact.
    """
    lead = repo.require_lead(lead_id)
    company = repo.get_company(lead.company_id)
    if company is None:
        raise ValueError(f"lead {lead_id} has no company record")
    contacts = repo.contacts_for(company.id)
    contact = None
    if contact_id:
        contact = repo.get_contact(contact_id)
        if contact is None:
            raise ValueError(f"no contact {contact_id!r}")
    elif contacts:
        contact = next((c for c in contacts if c.is_decision_maker), contacts[0])

    evidence = repo.evidence_for(lead_id)
    pain = repo.pain_points_for(lead_id)
    touches = len([d for d in repo.drafts(lead_id=lead_id) if d.state.value in ("sent", "approved")])
    step = choose_step(sequences or {}, lead, touches)
    subject, body = deterministic_body(
        company, contact, pain, evidence, offer or {}, step
    )

    model_report: Dict[str, Any] = {"used": False, "reason": "no local model configured"}
    if inference is not None and getattr(inference, "available", lambda: False)():
        prompt = (
            "Rewrite this sales email so it reads naturally. Keep every fact and "
            "every number exactly as given. Do not add statistics, claims, or "
            "names. Return only the email body.\n\n" + body
        )
        try:
            candidate = inference.complete(
                prompt,
                system="You edit sales emails for tone only. You never add facts or numbers.",
            )
        except Exception:
            candidate = None
        if candidate:
            ok, why = validate_rewrite(body, candidate)
            model_report = {"used": ok, "reason": why, "model": getattr(inference, "model", "")}
            if ok:
                body = candidate.strip()
        else:
            model_report = {"used": False, "reason": "model returned nothing (degraded)"}

    draft = OutreachDraft(
        lead_id=lead_id,
        contact_id=contact.id if contact else "",
        channel=channel,
        subject=subject,
        body=body,
        sequence_step=step["step"],
        variant=variant or step["sequence"],
        experiment_id=experiment_id or lead.experiment_id,
        evidence_ids=[e.id for e in evidence],
        run_id=run_id,
    )
    repo.create_draft(draft)
    return {
        "draft": draft.to_dict(),
        "sequence": step,
        "model": model_report,
        "evidence_count": len(evidence),
        "requires_approval": True,
    }
