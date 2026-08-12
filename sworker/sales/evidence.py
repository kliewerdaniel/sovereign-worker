"""Sales evidence — a thin wrapper over the existing ``EvidenceLedger``.

There is no second evidence system. This module:

  * validates that a sales claim carries a real ``source_ref`` and a known claim
    type before it is persisted (delegating the actual rule to the repository);
  * maps DailySalesOS claim-quality tiers (``Hypothesis_Log.md``) onto sworker
    ``Provenance`` values, in both directions, so a sales claim and a worker
    claim mean the same thing;
  * mirrors a sales evidence row into the run's ``EvidenceLedger`` when a run
    context exists, so ``audit <run_id>`` shows sales evidence alongside every
    other observation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..evidence import EvidenceLedger
from ..models import Provenance
from .models import CLAIM_TIER_ORDER, ClaimTier, SalesEvidenceRecord
from .repository import SalesRepository

# DailySalesOS tier -> sworker provenance.
TIER_TO_PROVENANCE: Dict[ClaimTier, Provenance] = {
    ClaimTier.CLAIM: Provenance.HYPOTHESIZED,
    ClaimTier.HYPOTHESIS: Provenance.INFERRED,
    ClaimTier.OBSERVED: Provenance.OBSERVED,
    ClaimTier.CLIENT_VERIFIED: Provenance.VERIFIED,
    ClaimTier.CASE_STUDY: Provenance.VERIFIED,
}

PROVENANCE_TO_TIER: Dict[Provenance, ClaimTier] = {
    Provenance.HYPOTHESIZED: ClaimTier.CLAIM,
    Provenance.INFERRED: ClaimTier.HYPOTHESIS,
    Provenance.OBSERVED: ClaimTier.OBSERVED,
    Provenance.RETRIEVED: ClaimTier.OBSERVED,
    Provenance.KNOWN: ClaimTier.HYPOTHESIS,
    Provenance.VERIFIED: ClaimTier.CLIENT_VERIFIED,
}

# Numeric confidence per tier. Deliberately conservative and fixed: confidence is
# a function of how the claim was obtained, never of how confident a model sounded.
TIER_CONFIDENCE: Dict[ClaimTier, float] = {
    ClaimTier.CLAIM: 0.15,
    ClaimTier.HYPOTHESIS: 0.4,
    ClaimTier.OBSERVED: 0.7,
    ClaimTier.CLIENT_VERIFIED: 0.9,
    ClaimTier.CASE_STUDY: 1.0,
}


def tier_of(value: "ClaimTier | str") -> ClaimTier:
    if isinstance(value, ClaimTier):
        return value
    key = str(value).strip().upper().replace(" ", "_")
    try:
        return ClaimTier(key)
    except ValueError:
        raise ValueError(
            f"unknown claim tier {value!r}; valid tiers (Hypothesis_Log.md): "
            f"{[t.value for t in CLAIM_TIER_ORDER]}"
        )


def tier_rank(value: "ClaimTier | str") -> int:
    return CLAIM_TIER_ORDER.index(tier_of(value))


def corroborated_tier(records: Sequence[SalesEvidenceRecord]) -> ClaimTier:
    """The tier a body of evidence collectively supports.

    CASE_STUDY requires CLIENT_VERIFIED evidence from >=2 independent sources —
    the same "independent source_ref" rule ``evidence.score`` already uses for
    worker claims. Nothing is promoted on volume alone.
    """
    if not records:
        return ClaimTier.CLAIM
    best = max(tier_rank(r.tier) for r in records)
    tier = CLAIM_TIER_ORDER[best]
    if tier is ClaimTier.CLIENT_VERIFIED:
        independent = {
            (r.source_ref or "").split("#")[0]
            for r in records
            if tier_of(r.tier) is ClaimTier.CLIENT_VERIFIED
        }
        if len(independent) >= 2:
            return ClaimTier.CASE_STUDY
    return tier


class SalesEvidence:
    """Attach sales claims to leads, mirrored into the run evidence ledger."""

    def __init__(self, repo: SalesRepository, ledger: Optional[EvidenceLedger] = None):
        self.repo = repo
        self.ledger = ledger

    def attach(
        self,
        lead_id: str,
        claim_type: str,
        claim_text: str,
        source_ref: str,
        *,
        tier: "ClaimTier | str" = ClaimTier.OBSERVED,
        excerpt: str = "",
        run_id: str = "",
        observation_id: str = "",
    ) -> SalesEvidenceRecord:
        t = tier_of(tier)
        rec = SalesEvidenceRecord(
            lead_id=lead_id,
            claim_type=claim_type,
            claim_text=claim_text,
            source_ref=source_ref,
            tier=t,
            excerpt=excerpt[:2000],
            run_id=run_id or (self.ledger.run_id if self.ledger else ""),
            observation_id=observation_id,
            confidence=TIER_CONFIDENCE[t],
        )
        if self.ledger is not None:
            mirrored = self.ledger.note(
                TIER_TO_PROVENANCE[t],
                f"[sales:{claim_type}] {claim_text}",
                source_ref=source_ref,
            )
            rec.worker_evidence_id = mirrored.id
        return self.repo.attach_evidence(rec)

    def list(self, lead_id: str, claim_type: str = "") -> List[SalesEvidenceRecord]:
        return self.repo.evidence_for(lead_id, claim_type)

    def explain(self, lead_id: str) -> Dict[str, Any]:
        """Answer 'why is this a good prospect, and what produced that answer?'
        entirely from stored data."""
        records = self.repo.evidence_for(lead_id)
        qual = self.repo.latest_qualification(lead_id)
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            by_type.setdefault(r.claim_type, []).append(
                {
                    "id": r.id,
                    "claim": r.claim_text,
                    "tier": r.tier.value if isinstance(r.tier, ClaimTier) else r.tier,
                    "source_ref": r.source_ref,
                    "confidence": r.confidence,
                    "excerpt": r.excerpt,
                }
            )
        return {
            "lead_id": lead_id,
            "evidence_count": len(records),
            "collective_tier": corroborated_tier(records).value,
            "by_claim_type": by_type,
            "score": qual.score if qual else 0.0,
            "score_version": qual.version if qual else 0,
            "reasoning": qual.reasoning if qual else "",
            "score_evidence_ids": list(qual.evidence_ids) if qual else [],
            "model": qual.model if qual else "",
        }
