"""The approval primitive (spec §4 + §45).

An approval is a durable record, not a prompt. The engine creates it, persists
it, and stops. Something else — a human at the CLI, or the web UI — decides.

The decision is appended to the audit trail and can never be edited.

§45 HITL escalation / quorum
-----------------------------
A single human is the historical floor, but some actions are consequential
enough that one "approve" click should not be the whole story. An approval can
require:

  * ``quorum``   — N *distinct* approvers before the action may proceed.
  * ``min_role`` — the minimum RBAC role any single vote must carry.

Enforcement rules (fail-closed, never trust the client description):
  * A vote whose ``role`` does not satisfy ``min_role`` is refused — it is never
    counted, and the approval stays PENDING.
  * The same person voting twice does not advance the quorum (distinct approvers
    only). Re-voting changes their prior stance but not the distinct count.
  * A single explicit REJECT blocks the action regardless of quorum — a quorum
    of approvals cannot out-vote one human saying no.
  * If the required quorum can no longer be reached (e.g. two approvers are
    needed but one already rejected), the approval settles to REJECTED
    immediately rather than hanging forever.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalState,
    RiskLevel,
    now,
)
from .store import WorkerStore
from .rbac import role_satisfies


class ApprovalError(Exception):
    """Refusal to record a vote (closed / under-role / already settled)."""


class ApprovalManager:
    def __init__(self, store: WorkerStore, worker=None):
        self.store = store
        self.worker = worker

    # -- creation ----------------------------------------------------------
    def request(
        self,
        action: Action,
        *,
        summary: str,
        reason: str,
        evidence_ids: Optional[List[str]] = None,
    ) -> Approval:
        # resolve §45 escalation requirements from the worker config (if given)
        quorum = 1
        min_role = ""
        if self.worker is not None:
            pol = self.worker.approval_policy_for(action.risk)
            quorum = pol["quorum"]
            min_role = pol["min_role"]
        appr = Approval(
            run_id=action.run_id,
            action_id=action.id,
            risk=action.risk,
            summary=summary,
            reason=reason,
            evidence_ids=list(evidence_ids or []),
            quorum=quorum,
            min_role=min_role,
        )
        self.store.put("approvals", appr, event="approval.requested")
        action.approval_id = appr.id
        action.status = ActionStatus.AWAITING_APPROVAL
        self.store.put("actions", action, event="action.awaiting_approval")
        return appr

    # -- voting (§45) ------------------------------------------------------
    def vote(
        self,
        approval_id: str,
        *,
        approved: bool,
        by: str,
        role: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Cast a single vote. Honors quorum + min_role; returns the record.

        Raises ``ApprovalError`` if the approval is closed (already settled),
        if ``role`` is below ``min_role``, or if ``by``/``role`` are missing for
        a decision that requires them.
        """
        rec = self.store.get("approvals", approval_id)
        if rec is None:
            raise KeyError(f"no approval {approval_id!r}")
        if rec["state"] != ApprovalState.PENDING.value:
            raise ApprovalError(
                f"approval {approval_id} is already {rec['state']}; approvals are immutable"
            )
        if not by:
            raise ApprovalError("a vote requires an identifiable approver (by)")
        # min_role gate — fail-closed: never count an under-privileged vote
        if not role_satisfies(role or "", rec.get("min_role", "") or ""):
            needed = rec.get("min_role") or "(any)"
            raise ApprovalError(
                f"approver role {role!r} does not satisfy required minimum {needed!r}"
            )

        state = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
        vote = {
            "state": state.value,
            "by": by,
            "role": role or "",
            "note": note or "",
            "at": now(),
        }
        votes = list(rec.get("votes", []))
        # replace any prior vote from the same approver (distinct approvers only)
        votes = [v for v in votes if v.get("by") != by]
        votes.append(vote)
        rec["votes"] = votes

        # a single rejection blocks, regardless of quorum
        if any(v["state"] == ApprovalState.REJECTED.value for v in votes):
            return self._settle(rec, ApprovalState.REJECTED, "quorum-reject")

        approvers = [v for v in votes if v["state"] == ApprovalState.APPROVED.value]
        need = max(1, int(rec.get("quorum", 1)))
        if len(approvers) >= need:
            return self._settle(rec, ApprovalState.APPROVED, "quorum-met")
        # quorum not yet met -> stays PENDING, no action state change
        self.store.put("approvals", rec, event="approval.voted")
        return self._strip(rec)

    def escalate(self, approval_id: str, by: str = "engine", note: str = "") -> Dict[str, Any]:
        """Raise the requirement when a vote cannot be honored (e.g. the only
        available approver is below ``min_role``). Voluntary structural
        escalation: bump quorum by one and keep min_role. Returns the record."""
        rec = self.store.get("approvals", approval_id)
        if rec is None:
            raise KeyError(f"no approval {approval_id!r}")
        if rec["state"] != ApprovalState.PENDING.value:
            raise ApprovalError(f"approval {approval_id} already {rec['state']}")
        rec["quorum"] = int(rec.get("quorum", 1)) + 1
        rec["escalations"] = int(rec.get("escalations", 0)) + 1
        self.store.put("approvals", rec, event="approval.escalated")
        return self._strip(rec)

    # -- single-vote shortcuts (preserve old behaviour) -------------------
    def approve(self, approval_id: str, by: str = "cli", role: str = "", note: str = "") -> Dict[str, Any]:
        return self.vote(approval_id, approved=True, by=by, role=role, note=note)

    def reject(self, approval_id: str, by: str = "cli", role: str = "", note: str = "") -> Dict[str, Any]:
        return self.vote(approval_id, approved=False, by=by, role=role, note=note)

    def decide(self, approval_id: str, *, approved: bool, by: str = "cli", role: str = "", note: str = "") -> Dict[str, Any]:
        return self.vote(approval_id, approved=approved, by=by, role=role, note=note)

    # -- helpers -----------------------------------------------------------
    def _settle(self, rec: Dict[str, Any], state: ApprovalState, reason: str) -> Dict[str, Any]:
        rec["state"] = state.value
        voters = rec.get("votes", [])
        rec["decided_by"] = ",".join(v["by"] for v in voters) or "none"
        rec["decided_at"] = now()
        rec["note"] = reason
        self.store.put("approvals", rec, event=f"approval.{state.value.lower()}")
        act = self.store.get("actions", rec["action_id"])
        if act:
            act["status"] = (
                ActionStatus.APPROVED.value
                if state is ApprovalState.APPROVED
                else ActionStatus.REJECTED.value
            )
            self.store.put("actions", act, event="action.decided")
        return self._strip(rec)

    @staticmethod
    def _strip(rec: Dict[str, Any]) -> Dict[str, Any]:
        # expose a stable view; callers must not mutate
        return dict(rec)

    def pending(self, run_id: str = "") -> List[Dict[str, Any]]:
        if run_id:
            return self.store.find(
                "approvals", order="created", state=ApprovalState.PENDING.value, run_id=run_id
            )
        return self.store.find("approvals", order="created", state=ApprovalState.PENDING.value)

    def for_run(self, run_id: str) -> List[Dict[str, Any]]:
        return self.store.find("approvals", run_id=run_id, order="created")

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("approvals", approval_id)

    def resolve_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        """Accept a full approval id, an action id, or a unique id prefix."""
        rec = self.store.get("approvals", ref)
        if rec:
            return rec
        by_action = self.store.find("approvals", action_id=ref)
        if by_action:
            return by_action[0]
        matches = [a for a in self.store.find("approvals") if a["id"].startswith(ref)]
        return matches[0] if len(matches) == 1 else None


def render_request(approval: Dict[str, Any], evidence: List[Dict[str, Any]]) -> str:
    req = ""
    if int(approval.get("quorum", 1)) > 1 or approval.get("min_role"):
        req = (
            f"  requires: {approval.get('quorum', 1)} approver(s)"
            + (f" at or above role '{approval['min_role']}'" if approval.get("min_role") else "")
            + "\n"
        )
    lines = [
        "ACTION REQUIRES APPROVAL",
        "",
        "Action:",
        f"  {approval['summary']}",
        "",
        "Reason:",
        f"  {approval['reason']}",
        "",
        "Risk:",
        f"  {approval['risk'].upper()}",
        req,
    ]
    if evidence:
        lines += ["", "Evidence:"]
        for e in evidence:
            lines.append(f"  - [{e['provenance']}] {e['summary']}")
            if e.get("source_ref"):
                lines.append(f"      source: {e['source_ref']}")
    lines += [
        "",
        f"  approve:  worker approve {approval['id']}",
        f"  reject:   worker reject  {approval['id']}",
    ]
    return "\n".join(lines)
