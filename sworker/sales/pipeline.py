"""Pipeline stages and legal transitions — compiled from ``CRM_Pipeline.md``.

The document's stage list is the canonical vocabulary; this module encodes it as
a state machine so a stage change is a *checked* operation rather than a free-text
field update. It also carries the document's own hygiene rules (max duration per
stage) so ``followups`` can compute what is overdue deterministically.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import PipelineStage as S

STAGES: List[S] = list(S)

# Mapping back to the numbering used in CRM_Pipeline.md. Stage 10 in the document
# is the single heading "Won / Lost"; both terminal outcomes map to 10.
DOC_STAGE_NUMBERS: Dict[S, int] = {
    S.PROSPECT: 1,
    S.CONTACTED: 2,
    S.RESPONDED: 3,
    S.DISCOVERY_SCHEDULED: 4,
    S.DISCOVERY_COMPLETED: 5,
    S.QUALIFIED: 6,
    S.AUDIT_IN_PROGRESS: 7,
    S.PROPOSAL_SENT: 8,
    S.NEGOTIATION: 9,
    S.WON: 10,
    S.LOST: 10,
    S.ONBOARDING: 11,
    S.IMPLEMENTATION: 12,
    S.COMPLETED: 13,
    S.EXPANSION: 14,
}

# Max duration in a stage, in days, from the document's stage table / hygiene
# rules. 0 = no documented cap.
MAX_DAYS_IN_STAGE: Dict[S, int] = {
    S.PROSPECT: 0,
    S.CONTACTED: 7,
    S.RESPONDED: 3,
    S.DISCOVERY_SCHEDULED: 0,
    S.DISCOVERY_COMPLETED: 1,     # "follow-up within 24h"
    S.QUALIFIED: 5,
    S.AUDIT_IN_PROGRESS: 14,
    S.PROPOSAL_SENT: 21,
    S.NEGOTIATION: 0,
    S.WON: 0,
    S.LOST: 0,
    S.ONBOARDING: 0,
    S.IMPLEMENTATION: 0,
    S.COMPLETED: 0,
    S.EXPANSION: 0,
}

# Entry criteria text, verbatim-ish from the document, surfaced in errors and UI
# so a rejected transition explains itself against the source of truth.
ENTRY_CRITERIA: Dict[S, str] = {
    S.PROSPECT: "Name, company, source recorded",
    S.CONTACTED: "First outreach sent",
    S.RESPONDED: "Any reply received (even no)",
    S.DISCOVERY_SCHEDULED: "Agreed to discovery call; call date/time known",
    S.DISCOVERY_COMPLETED: "Call done; notes, pain points, score, next step recorded",
    S.QUALIFIED: "Meets audit criteria (50+ leads/month, decision maker, budget)",
    S.AUDIT_IN_PROGRESS: "Signed agreement + payment received",
    S.PROPOSAL_SENT: "Audit delivered, implementation proposed",
    S.NEGOTIATION: "Client asking questions / negotiating",
    S.WON: "Agreement signed",
    S.LOST: "Explicit loss; reason code + re-engage date required",
    S.ONBOARDING: "Contract signed + payment received",
    S.IMPLEMENTATION: "Kickoff complete; active project phase",
    S.COMPLETED: "Project delivered + accepted",
    S.EXPANSION: "Upsell / retainer opportunity",
}

# Legal forward transitions. LOST is reachable from any non-terminal stage
# (a deal can die at any point); nothing may move *out* of a terminal stage
# except LOST -> PROSPECT (documented "re-engage date" nurture path) and
# WON -> ONBOARDING.
_FORWARD: Dict[S, Tuple[S, ...]] = {
    S.PROSPECT: (S.CONTACTED,),
    S.CONTACTED: (S.RESPONDED,),
    S.RESPONDED: (S.DISCOVERY_SCHEDULED, S.QUALIFIED),
    S.DISCOVERY_SCHEDULED: (S.DISCOVERY_COMPLETED,),
    S.DISCOVERY_COMPLETED: (S.QUALIFIED,),
    S.QUALIFIED: (S.AUDIT_IN_PROGRESS, S.PROPOSAL_SENT),
    S.AUDIT_IN_PROGRESS: (S.PROPOSAL_SENT,),
    S.PROPOSAL_SENT: (S.NEGOTIATION, S.WON),
    S.NEGOTIATION: (S.WON,),
    S.WON: (S.ONBOARDING,),
    S.LOST: (S.PROSPECT,),
    S.ONBOARDING: (S.IMPLEMENTATION,),
    S.IMPLEMENTATION: (S.COMPLETED,),
    S.COMPLETED: (S.EXPANSION,),
    S.EXPANSION: (),
}

_TERMINAL = (S.WON, S.LOST, S.COMPLETED, S.EXPANSION)

# Stages from which a deal may be marked LOST.
_LOSABLE = tuple(s for s in STAGES if s not in (S.WON, S.LOST, S.COMPLETED))


def stage_of(value: "S | str") -> S:
    """Coerce to a PipelineStage, raising a helpful error for unknown values."""
    if isinstance(value, S):
        return value
    key = str(value).strip().lower().replace(" ", "_").replace("/", "_")
    try:
        return S(key)
    except ValueError:
        raise ValueError(
            f"unknown pipeline stage {value!r}; valid stages (CRM_Pipeline.md): "
            f"{[s.value for s in STAGES]}"
        )


def stage_index(value: "S | str") -> int:
    return STAGES.index(stage_of(value))


def allowed_next(value: "S | str") -> List[S]:
    cur = stage_of(value)
    out = list(_FORWARD.get(cur, ()))
    if cur in _LOSABLE and S.LOST not in out:
        out.append(S.LOST)
    return out


def can_move(src: "S | str", dst: "S | str") -> Tuple[bool, str]:
    """Is this transition legal? Returns (ok, reason).

    Fails closed: an undocumented jump is refused with the entry criteria of the
    target stage, so the caller learns what is actually missing.
    """
    a, b = stage_of(src), stage_of(dst)
    if a is b:
        return False, f"lead is already in stage {a.value!r}"
    if b in allowed_next(a):
        return True, f"{a.value} -> {b.value} permitted"
    return False, (
        f"{a.value} -> {b.value} is not a documented transition "
        f"(allowed from {a.value}: {[s.value for s in allowed_next(a)]}); "
        f"entry criteria for {b.value}: {ENTRY_CRITERIA.get(b, '?')}"
    )


def is_terminal(value: "S | str") -> bool:
    return stage_of(value) in _TERMINAL


def days_overdue(stage: "S | str", days_in_stage: float) -> float:
    """How far past the documented max duration this lead is. 0 when within."""
    cap = MAX_DAYS_IN_STAGE.get(stage_of(stage), 0)
    if not cap:
        return 0.0
    over = days_in_stage - cap
    return round(over, 3) if over > 0 else 0.0


def describe() -> List[Dict[str, object]]:
    """Machine-readable projection of CRM_Pipeline.md, for the API/UI."""
    return [
        {
            "stage": s.value,
            "doc_stage": DOC_STAGE_NUMBERS[s],
            "entry_criteria": ENTRY_CRITERIA.get(s, ""),
            "max_days": MAX_DAYS_IN_STAGE.get(s, 0),
            "allowed_next": [n.value for n in allowed_next(s)],
            "terminal": is_terminal(s),
        }
        for s in STAGES
    ]
