"""Sales ontology — a compiled, queryable projection of the DailySalesOS markdown.

The markdown docs stay the human-readable source of truth for offer, ICP and
process. These dataclasses are the machine-readable projection of the concepts
they already describe, persisted by ``repository.py`` into an *extension* of the
existing ``Experiment_Ledger`` sqlite schema.

Design rules, matching sworker's own discipline:
  * plain stdlib dataclasses, no third-party validation library;
  * enums for anything with a documented fixed vocabulary (pipeline stages come
    from ``CRM_Pipeline.md``, claim tiers from ``Hypothesis_Log.md``);
  * every record carries ``created``/``updated`` and, where a number is asserted,
    the evidence ids that produced it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------


class PipelineStage(str, Enum):
    """The 14 stages defined in DailySalesOS ``CRM_Pipeline.md``.

    The document numbers 14 stages, but its stage 10 heading is "Won / Lost" —
    one heading covering two terminal outcomes with different downstream
    behaviour (Won → Onboarding, Lost → capture reason + re-engage date). They
    are therefore distinct enum members here, giving 15 members over the 14
    documented stages. ``pipeline.DOC_STAGE_NUMBERS`` records the mapping back
    to the document's numbering so the projection stays traceable.
    """

    PROSPECT = "prospect"
    CONTACTED = "contacted"
    RESPONDED = "responded"
    DISCOVERY_SCHEDULED = "discovery_scheduled"
    DISCOVERY_COMPLETED = "discovery_completed"
    QUALIFIED = "qualified"
    AUDIT_IN_PROGRESS = "audit_in_progress"
    PROPOSAL_SENT = "proposal_sent"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"
    ONBOARDING = "onboarding"
    IMPLEMENTATION = "implementation"
    COMPLETED = "completed"
    EXPANSION = "expansion"


class ClaimTier(str, Enum):
    """Claim-quality tiers from DailySalesOS ``Hypothesis_Log.md``.

    Ordered weakest -> strongest. ``OBSERVED`` explicitly means measured with
    insufficient sample size (n<50) in the source document.
    """

    CLAIM = "CLAIM"
    HYPOTHESIS = "HYPOTHESIS"
    OBSERVED = "OBSERVED"
    CLIENT_VERIFIED = "CLIENT_VERIFIED"
    CASE_STUDY = "CASE_STUDY"


CLAIM_TIER_ORDER: List[ClaimTier] = [
    ClaimTier.CLAIM,
    ClaimTier.HYPOTHESIS,
    ClaimTier.OBSERVED,
    ClaimTier.CLIENT_VERIFIED,
    ClaimTier.CASE_STUDY,
]

# Sales claim types that may carry evidence. Anything outside this set is
# refused rather than stored under a made-up type (fail closed).
SALES_CLAIM_TYPES = (
    "pain_point",
    "icp_fit",
    "contact_info",
    "size_signal",
    "tech_signal",
    "hiring_signal",
    "urgency_signal",
    "budget_signal",
    "operational_signal",
    "provenance",
)


class OutreachState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


class TaskState(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


class SalesRecord:
    """dict <-> dataclass with enums flattened, mirroring models.Record."""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in asdict(self).items():  # type: ignore[arg-type]
            out[k] = v.value if isinstance(v, Enum) else v
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in d.items() if k in names})  # type: ignore[call-arg]


@dataclass
class Company(SalesRecord):
    name: str
    id: str = field(default_factory=lambda: new_id("co"))
    domain: str = ""
    industry: str = ""
    geography: str = ""
    team_size: int = 0
    description: str = ""
    website: str = ""
    source: str = ""
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)


@dataclass
class Contact(SalesRecord):
    company_id: str
    name: str
    id: str = field(default_factory=lambda: new_id("ct"))
    role: str = ""
    email: str = ""
    phone: str = ""
    is_decision_maker: bool = False
    source: str = ""
    created: float = field(default_factory=now)


@dataclass
class Lead(SalesRecord):
    """A candidate the system is working. One lead per company per ICP cycle."""

    company_id: str
    id: str = field(default_factory=lambda: new_id("lead"))
    prospect_id: str = ""          # link back to the pre-existing `prospects` row
    stage: PipelineStage = PipelineStage.PROSPECT
    source: str = ""
    dedupe_key: str = ""           # normalised company identity; UNIQUE in schema
    score: float = 0.0             # latest qualification score (0-100)
    score_version: int = 0
    owner: str = ""
    experiment_id: str = ""
    lost_reason: str = ""
    next_action: str = ""
    next_action_due: str = ""      # ISO date
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)


@dataclass
class Activity(SalesRecord):
    """Something that happened to a lead. Append-only by convention."""

    lead_id: str
    kind: str                      # research | outreach | reply | call | note | stage_change
    summary: str
    id: str = field(default_factory=lambda: new_id("act"))
    run_id: str = ""               # the sworker run that produced it, if any
    worker: str = ""
    detail: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    created: float = field(default_factory=now)


@dataclass
class Opportunity(SalesRecord):
    lead_id: str
    id: str = field(default_factory=lambda: new_id("opp"))
    name: str = ""
    value: float = 0.0
    currency: str = "USD"
    stage: PipelineStage = PipelineStage.QUALIFIED
    probability: float = 0.0
    close_date: str = ""
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)


@dataclass
class SalesEvidenceRecord(SalesRecord):
    """Evidence for one sales claim about one lead.

    ``source_ref`` is mandatory and is a real locator (``path#sha256:...``,
    ``obs_...``, ``atlas claim id``). A row without one is refused by the
    repository — this is the same rule the sworker EvidenceLedger enforces.
    """

    lead_id: str
    claim_type: str
    claim_text: str
    source_ref: str
    id: str = field(default_factory=lambda: new_id("sev"))
    tier: ClaimTier = ClaimTier.CLAIM
    excerpt: str = ""
    run_id: str = ""
    observation_id: str = ""
    worker_evidence_id: str = ""   # id of the mirrored sworker Evidence row
    confidence: float = 0.0
    created: float = field(default_factory=now)


@dataclass
class PainPoint(SalesRecord):
    lead_id: str
    text: str
    id: str = field(default_factory=lambda: new_id("pp"))
    category: str = ""             # from Discovery_Rubric.md categories
    severity: int = 0              # 1-5 (Pain Level)
    frequency: int = 0             # 1-5
    revenue_impact: int = 0        # 1-5
    automation_potential: int = 0  # 1-5
    implementation_difficulty: int = 1  # 1-5, never 0 (divisor)
    opportunity_score: float = 0.0      # rubric formula, normalised 0-100
    tier: ClaimTier = ClaimTier.CLAIM
    evidence_ids: List[str] = field(default_factory=list)
    created: float = field(default_factory=now)


@dataclass
class Qualification(SalesRecord):
    """An append-only scoring record. Re-scoring inserts a new version."""

    lead_id: str
    id: str = field(default_factory=lambda: new_id("qual"))
    version: int = 1
    icp_fit: float = 0.0
    pain_signal: float = 0.0
    urgency: float = 0.0
    economic_potential: float = 0.0
    accessibility: float = 0.0
    confidence: float = 0.0
    score: float = 0.0
    tier: ClaimTier = ClaimTier.CLAIM
    signals: Dict[str, Any] = field(default_factory=dict)
    evidence_ids: List[str] = field(default_factory=list)
    reasoning: str = ""
    model: str = "deterministic"   # "deterministic" or the model id that assisted
    model_version: str = ""
    run_id: str = ""
    created: float = field(default_factory=now)


@dataclass
class OutreachDraft(SalesRecord):
    lead_id: str
    channel: str
    subject: str
    body: str
    id: str = field(default_factory=lambda: new_id("out"))
    contact_id: str = ""
    state: OutreachState = OutreachState.DRAFT
    sequence_step: str = ""        # e.g. "day_0" from Follow_Up_System.md
    variant: str = ""
    experiment_id: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    approved_by: str = ""
    approved_at: float = 0.0
    sent_at: float = 0.0
    receipt: str = ""
    run_id: str = ""
    created: float = field(default_factory=now)


@dataclass
class Task(SalesRecord):
    lead_id: str
    title: str
    id: str = field(default_factory=lambda: new_id("task"))
    kind: str = "generic"
    due: str = ""                  # ISO date
    state: TaskState = TaskState.OPEN
    detail: str = ""
    run_id: str = ""
    completed_at: float = 0.0
    created: float = field(default_factory=now)


@dataclass
class FollowUp(SalesRecord):
    lead_id: str
    due: str                       # ISO date
    id: str = field(default_factory=lambda: new_id("fu"))
    reason: str = ""
    sequence: str = ""             # which Follow_Up_System.md sequence
    step: str = ""                 # which step within it
    state: TaskState = TaskState.OPEN
    run_id: str = ""
    completed_at: float = 0.0
    created: float = field(default_factory=now)


@dataclass
class ICP(SalesRecord):
    """The ideal customer profile, compiled from Industry_Ranking.md + Core_Offer.md."""

    name: str
    id: str = field(default_factory=lambda: new_id("icp"))
    industry: str = ""
    min_team_size: int = 0
    geography: str = ""
    rank: int = 0
    rank_score: float = 0.0
    offer: str = ""
    offer_price: float = 0.0
    source_doc: str = ""
    active: bool = True
    created: float = field(default_factory=now)


@dataclass
class Proposal(SalesRecord):
    lead_id: str
    id: str = field(default_factory=lambda: new_id("prop"))
    value: float = 0.0
    sent_date: str = ""
    state: str = "draft"
    detail: str = ""
    created: float = field(default_factory=now)


@dataclass
class Outcome(SalesRecord):
    lead_id: str
    result: str                    # won | lost
    id: str = field(default_factory=lambda: new_id("outc"))
    reason: str = ""
    value: float = 0.0
    revisit_date: str = ""
    created: float = field(default_factory=now)


@dataclass
class PipelineTransition(SalesRecord):
    """Immutable stage-change history. Never updated, only appended."""

    lead_id: str
    from_stage: str
    to_stage: str
    id: str = field(default_factory=lambda: new_id("pt"))
    reason: str = ""
    run_id: str = ""
    worker: str = ""
    created: float = field(default_factory=now)


Interaction = Activity  # DailySalesOS uses both words for the same object
