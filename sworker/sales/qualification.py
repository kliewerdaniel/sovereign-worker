"""Qualification scoring — deterministic first, LLM-assisted only for prose.

Every number here is computed with stdlib arithmetic from evidence rows that a
tool actually observed. A language model may add a *summary*; it can never move a
sub-score, and its output is stored as a HYPOTHESIZED claim with no evidence, so
it cannot masquerade as a finding.

Sub-scores are 0-100 each and combined by the weights below:

    lead_score = ICP_fit + pain_signal + urgency + economic_potential
               + accessibility + confidence   (weighted mean, 0-100)

Pain signal uses the opportunity-score formula already defined in
``Discovery_Rubric.md``:

    (Pain × Frequency × Revenue Impact × Automation Potential)
        / Implementation Difficulty          normalised to 0-100

so the sales team's own rubric — not a new invention — drives the number.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .evidence import TIER_CONFIDENCE, corroborated_tier, tier_of
from .models import (
    ClaimTier,
    Company,
    ICP,
    Lead,
    PainPoint,
    Qualification,
    SalesEvidenceRecord,
)
from .repository import SalesRepository

# Weights sum to 1.0. Chosen to match the ordering the DailySalesOS docs imply:
# ICP fit and pain dominate, confidence is a multiplier-like discount factor.
WEIGHTS: Dict[str, float] = {
    "icp_fit": 0.25,
    "pain_signal": 0.25,
    "urgency": 0.15,
    "economic_potential": 0.15,
    "accessibility": 0.10,
    "confidence": 0.10,
}

# Max value of the raw rubric formula: 5*5*5*5 / 1 = 625.
_RUBRIC_MAX = 625.0

# Which claim types feed which sub-score.
_URGENCY_TYPES = ("urgency_signal", "hiring_signal")
_ECONOMIC_TYPES = ("budget_signal", "size_signal")
_ACCESS_TYPES = ("contact_info",)


class InsufficientEvidence(Exception):
    """Raised when a score was requested for a lead with no evidence at all.

    Deliberately an exception rather than a zero score: a 0 would look like a
    measured judgement about a bad prospect, when in fact nothing is known.
    """


def opportunity_score(pp: PainPoint) -> float:
    """The Discovery_Rubric.md formula, normalised 0-100. Pure arithmetic."""
    difficulty = max(1, int(pp.implementation_difficulty or 1))
    raw = (
        max(0, int(pp.severity))
        * max(0, int(pp.frequency))
        * max(0, int(pp.revenue_impact))
        * max(0, int(pp.automation_potential))
    ) / difficulty
    if not raw:
        return 0.0
    return round(min(100.0, raw / _RUBRIC_MAX * 100.0), 4)


def score_icp_fit(company: Company, icps: List[ICP]) -> Tuple[float, Dict[str, Any]]:
    """How well the company matches an active ICP. 0-100, explainable.

    Industry match is the dominant term because ``Industry_Ranking.md`` ranks by
    industry; team size and geography refine it.
    """
    if not icps:
        return 0.0, {"reason": "no active ICP configured; run `sworker sales init`"}
    best = 0.0
    detail: Dict[str, Any] = {}
    for icp in icps:
        parts: Dict[str, float] = {}
        industry_match = bool(
            icp.industry
            and company.industry
            and (
                icp.industry.lower() in company.industry.lower()
                or company.industry.lower() in icp.industry.lower()
            )
        )
        parts["industry"] = 55.0 if industry_match else 0.0
        if icp.min_team_size:
            if company.team_size >= icp.min_team_size:
                parts["team_size"] = 25.0
            elif company.team_size:
                parts["team_size"] = round(
                    25.0 * company.team_size / icp.min_team_size, 2
                )
            else:
                parts["team_size"] = 0.0
        else:
            parts["team_size"] = 12.5 if company.team_size else 0.0
        if icp.geography and company.geography:
            parts["geography"] = (
                20.0 if icp.geography.lower() in company.geography.lower() else 0.0
            )
        else:
            parts["geography"] = 10.0  # unknown geography is not a penalty
        total = round(sum(parts.values()), 2)
        # Rank discount: ICP #1 keeps full weight, #2 loses 5%, etc.
        rank_factor = 1.0 - min(0.3, 0.05 * max(0, (icp.rank or 1) - 1))
        total = round(total * rank_factor, 2)
        if total > best:
            best = total
            detail = {
                "icp": icp.name,
                "icp_rank": icp.rank,
                "rank_factor": rank_factor,
                "components": parts,
                "source_doc": icp.source_doc,
            }
    return min(100.0, best), detail


def score_pain(pain_points: List[PainPoint]) -> Tuple[float, Dict[str, Any]]:
    """Max rubric opportunity score across recorded pain points, plus breadth."""
    if not pain_points:
        return 0.0, {"reason": "no pain points recorded with evidence"}
    scores = [opportunity_score(p) for p in pain_points]
    top = max(scores)
    breadth = min(20.0, 5.0 * (len(pain_points) - 1))
    total = round(min(100.0, top + breadth), 2)
    return total, {
        "top_opportunity_score": top,
        "pain_point_count": len(pain_points),
        "breadth_bonus": breadth,
        "formula": "(Pain x Frequency x RevenueImpact x AutomationPotential) / Difficulty",
        "source_doc": "Discovery_Rubric.md",
    }


def _signal_score(
    evidence: List[SalesEvidenceRecord], types: Tuple[str, ...]
) -> Tuple[float, Dict[str, Any]]:
    """Score a signal family from tier-weighted evidence. 0-100."""
    hits = [e for e in evidence if e.claim_type in types]
    if not hits:
        return 0.0, {"claim_types": list(types), "evidence_count": 0}
    weights = [TIER_CONFIDENCE[tier_of(e.tier)] for e in hits]
    independent = len({(e.source_ref or "").split("#")[0] for e in hits})
    strength = max(weights)
    corroboration = min(0.3, 0.1 * (independent - 1))
    return round(min(100.0, (strength + corroboration) * 100.0), 2), {
        "claim_types": list(types),
        "evidence_count": len(hits),
        "independent_sources": independent,
        "strongest_tier": max(hits, key=lambda e: TIER_CONFIDENCE[tier_of(e.tier)]).tier,
        "evidence_ids": [e.id for e in hits],
    }


def evaluate(
    repo: SalesRepository,
    lead_id: str,
    *,
    run_id: str = "",
    inference: Any = None,
) -> Qualification:
    """Score a lead from stored evidence. Append-only; never overwrites history.

    ``inference`` is optional and may only contribute ``reasoning`` prose. When it
    is absent or unavailable (``NullInference``) the numbers are byte-identical —
    the degradation contract in ``degradation.py``.
    """
    lead: Lead = repo.require_lead(lead_id)
    company = repo.get_company(lead.company_id)
    if company is None:
        raise InsufficientEvidence(f"lead {lead_id} has no company record")
    evidence = repo.evidence_for(lead_id)
    pain_points = repo.pain_points_for(lead_id)
    if not evidence:
        raise InsufficientEvidence(
            f"lead {lead_id} has no evidence; a score must be explainable from "
            "evidence.list() — research the lead before qualifying it"
        )

    icp_fit, icp_detail = score_icp_fit(company, repo.active_icp())
    pain_signal, pain_detail = score_pain(pain_points)
    urgency, urgency_detail = _signal_score(evidence, _URGENCY_TYPES)
    economic, economic_detail = _signal_score(evidence, _ECONOMIC_TYPES)
    access, access_detail = _signal_score(evidence, _ACCESS_TYPES)
    contacts = repo.contacts_for(company.id)
    if any(c.is_decision_maker for c in contacts):
        access = min(100.0, access + 25.0)
        access_detail["decision_maker_known"] = True

    tier = corroborated_tier(evidence)
    confidence = round(TIER_CONFIDENCE[tier] * 100.0, 2)

    subs = {
        "icp_fit": icp_fit,
        "pain_signal": pain_signal,
        "urgency": urgency,
        "economic_potential": economic,
        "accessibility": access,
        "confidence": confidence,
    }
    score = round(sum(subs[k] * WEIGHTS[k] for k in WEIGHTS), 2)

    signals = {
        "icp_fit": icp_detail,
        "pain_signal": pain_detail,
        "urgency": urgency_detail,
        "economic_potential": economic_detail,
        "accessibility": access_detail,
        "confidence": {"collective_tier": tier.value, "evidence_count": len(evidence)},
        "weights": dict(WEIGHTS),
    }

    reasoning = deterministic_reasoning(company, subs, score, tier, len(evidence))
    model = "deterministic"
    model_version = ""
    if inference is not None and getattr(inference, "available", lambda: False)():
        prose = _model_summary(inference, company, subs, score, evidence, pain_points)
        if prose:
            reasoning = f"{reasoning}\n\n[model summary, HYPOTHESIZED, not evidence]\n{prose}"
            model = f"assisted:{getattr(inference, 'model', 'local')}"
            model_version = str(getattr(inference, "base_url", ""))

    qual = Qualification(
        lead_id=lead_id,
        version=repo.next_qualification_version(lead_id),
        icp_fit=icp_fit,
        pain_signal=pain_signal,
        urgency=urgency,
        economic_potential=economic,
        accessibility=access,
        confidence=confidence,
        score=score,
        tier=tier,
        signals=signals,
        evidence_ids=[e.id for e in evidence],
        reasoning=reasoning,
        model=model,
        model_version=model_version,
        run_id=run_id,
    )
    return repo.record_qualification(qual)


def deterministic_reasoning(
    company: Company,
    subs: Dict[str, float],
    score: float,
    tier: ClaimTier,
    evidence_count: int,
) -> str:
    parts = ", ".join(f"{k}={v}" for k, v in subs.items())
    return (
        f"{company.name}: score {score}/100 from {parts} "
        f"(weights {dict(WEIGHTS)}); {evidence_count} evidence row(s), "
        f"collective claim tier {tier.value}. Every sub-score is re-derivable "
        f"from evidence.list() for this lead."
    )


def _model_summary(
    inference: Any,
    company: Company,
    subs: Dict[str, float],
    score: float,
    evidence: List[SalesEvidenceRecord],
    pain_points: List[PainPoint],
) -> Optional[str]:
    lines = [f"- [{e.claim_type}/{e.tier}] {e.claim_text}" for e in evidence[:20]]
    pain = [f"- {p.text} (opportunity {p.opportunity_score})" for p in pain_points[:10]]
    prompt = (
        "Summarise, in at most four sentences, why this company is or is not a good "
        "prospect. Use ONLY the evidence listed. Do not introduce new facts, "
        "numbers, or names.\n\n"
        f"Company: {company.name} ({company.industry or 'industry unknown'}, "
        f"team size {company.team_size or 'unknown'})\n"
        f"Computed score: {score}/100 with sub-scores {subs}\n"
        f"Evidence:\n" + "\n".join(lines) + "\nPain points:\n" + "\n".join(pain)
    )
    try:
        return inference.complete(
            prompt,
            system=(
                "You summarise sales evidence. You never assert a fact that is not "
                "in the supplied evidence. You never produce numbers of your own."
            ),
        )
    except Exception:
        return None


def score_breakdown(repo: SalesRepository, lead_id: str) -> Dict[str, Any]:
    """Recompute the weighted total from the stored sub-scores.

    Used by the ``sales_score_recomputes`` verification check: if the stored total
    disagrees with the arithmetic, the run degrades rather than keeping the
    friendlier number.
    """
    qual = repo.latest_qualification(lead_id)
    if qual is None:
        return {"lead_id": lead_id, "found": False}
    subs = {
        "icp_fit": qual.icp_fit,
        "pain_signal": qual.pain_signal,
        "urgency": qual.urgency,
        "economic_potential": qual.economic_potential,
        "accessibility": qual.accessibility,
        "confidence": qual.confidence,
    }
    recomputed = round(sum(subs[k] * WEIGHTS[k] for k in WEIGHTS), 2)
    return {
        "lead_id": lead_id,
        "found": True,
        "version": qual.version,
        "sub_scores": subs,
        "weights": dict(WEIGHTS),
        "stored_score": qual.score,
        "recomputed_score": recomputed,
        "matches": abs(recomputed - qual.score) < 0.011,
        "evidence_ids": list(qual.evidence_ids),
    }
