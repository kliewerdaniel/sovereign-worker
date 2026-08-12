"""Follow-up rules — the next action per stage, from ``Follow_Up_System.md``.

Deterministic: given a lead's stage, the age of its last activity and the
documented sequence, this module says what should happen next and when. No model
decides whether a prospect gets chased.

The rule set is the one the document states:
  no response        -> follow-up
  positive response  -> discovery task
  proposal sent      -> proposal follow-up
  meeting completed  -> next-step task
  lost               -> nurture / revisit date
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import FollowUp, PipelineStage as S, Task
from .pipeline import MAX_DAYS_IN_STAGE, days_overdue, stage_of
from .repository import SalesRepository

# stage -> (reason, task_kind, default day offset)
RULES: Dict[S, Dict[str, Any]] = {
    S.CONTACTED: {
        "reason": "no response to initial outreach",
        "kind": "followup",
        "days": 3,
        "sequence": "New Prospect",
    },
    S.RESPONDED: {
        "reason": "positive response — book discovery",
        "kind": "discovery",
        "days": 1,
        "sequence": "New Prospect",
    },
    S.DISCOVERY_SCHEDULED: {
        "reason": "prep research before the call",
        "kind": "prep",
        "days": 1,
        "sequence": "New Prospect",
    },
    S.DISCOVERY_COMPLETED: {
        "reason": "send call summary + audit proposal within 24h",
        "kind": "next_step",
        "days": 1,
        "sequence": "Discovery Completed",
    },
    S.QUALIFIED: {
        "reason": "send audit proposal",
        "kind": "proposal",
        "days": 2,
        "sequence": "Discovery Completed",
    },
    S.PROPOSAL_SENT: {
        "reason": "proposal follow-up",
        "kind": "followup",
        "days": 3,
        "sequence": "Proposal Sent",
    },
    S.NEGOTIATION: {
        "reason": "address open objections",
        "kind": "next_step",
        "days": 2,
        "sequence": "Proposal Sent",
    },
    S.AUDIT_IN_PROGRESS: {
        "reason": "audit delivery checkpoint",
        "kind": "delivery",
        "days": 7,
        "sequence": "Discovery Completed",
    },
    S.LOST: {
        "reason": "nurture / revisit",
        "kind": "nurture",
        "days": 90,
        "sequence": "New Prospect",
    },
}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def next_action_for(
    stage: "S | str",
    *,
    sequences: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    touches: int = 0,
    from_day: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """What should happen next for a lead in this stage, and when.

    When the document's sequence for the stage is available, the day offset comes
    from the sequence step rather than the fallback in RULES — so editing the
    markdown changes the behaviour without touching code.
    """
    st = stage_of(stage)
    rule = RULES.get(st)
    if rule is None:
        return None
    base = from_day or _today()
    days = int(rule["days"])
    step_name = f"day_{days}"
    action = rule["reason"]
    seq_steps = (sequences or {}).get(rule["sequence"]) or []
    if seq_steps:
        idx = min(touches, len(seq_steps) - 1)
        chosen = seq_steps[idx]
        days = int(chosen["day"]) or days
        step_name = f"day_{chosen['day']}"
        action = chosen["action"]
    return {
        "stage": st.value,
        "reason": rule["reason"],
        "kind": rule["kind"],
        "sequence": rule["sequence"],
        "step": step_name,
        "action": action,
        "due": (base + timedelta(days=days)).isoformat(),
        "max_days_in_stage": MAX_DAYS_IN_STAGE.get(st, 0),
    }


def schedule_for_lead(
    repo: SalesRepository,
    lead_id: str,
    *,
    sequences: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    run_id: str = "",
) -> Dict[str, Any]:
    """Schedule the documented next action for one lead, idempotently.

    If an open follow-up already exists for the lead, nothing new is created —
    re-running the daily procedure must not multiply reminders.
    """
    lead = repo.require_lead(lead_id)
    existing = [f for f in repo.due_followups(on="9999-12-31") if f.lead_id == lead_id]
    if existing:
        return {
            "lead_id": lead_id,
            "created": False,
            "reason": "an open follow-up already exists",
            "followup_id": existing[0].id,
        }
    touches = len([d for d in repo.drafts(lead_id=lead_id) if d.state.value in ("sent", "approved")])
    plan = next_action_for(lead.stage, sequences=sequences, touches=touches)
    if plan is None:
        return {
            "lead_id": lead_id,
            "created": False,
            "reason": f"no documented follow-up rule for stage {lead.stage}",
        }
    fu = repo.schedule_followup(
        FollowUp(
            lead_id=lead_id,
            due=plan["due"],
            reason=plan["reason"],
            sequence=plan["sequence"],
            step=plan["step"],
            run_id=run_id,
        )
    )
    task = repo.create_task(
        Task(
            lead_id=lead_id,
            title=plan["action"],
            kind=plan["kind"],
            due=plan["due"],
            detail=f"{plan['sequence']} / {plan['step']}",
            run_id=run_id,
        )
    )
    return {
        "lead_id": lead_id,
        "created": True,
        "followup_id": fu.id,
        "task_id": task.id,
        "plan": plan,
    }


def due_today(repo: SalesRepository, on: str = "") -> Dict[str, Any]:
    """Everything that needs a human or a worker today, with overdue leads."""
    day = on or _today().isoformat()
    followups = repo.due_followups(on=day)
    tasks = [t for t in repo.open_tasks() if (t.due or "9999") <= day]
    stale = repo.stale_leads()
    return {
        "date": day,
        "followups": [f.to_dict() for f in followups],
        "tasks": [t.to_dict() for t in tasks],
        "stale_leads": stale,
        "counts": {
            "followups_due": len(followups),
            "tasks_due": len(tasks),
            "leads_past_stage_sla": len(stale),
        },
    }
