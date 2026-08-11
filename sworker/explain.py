"""§28 explainability / §29 dry-run / §30 replay distinction.

These three product capabilities share one engine entry point — ``explain`` —
that plans a request and evaluates the permission engine for every step but
NEVER executes a tool. That makes dry-run a real safety surface (the model
proposes, the engine disposes, and you can see the disposition before anything
runs), and it underpins replay.

  * ``explain(engine, request, ...)`` -> ``ExplainResult``: intent + per-step
    disposition (executed / denied / awaited) derived from the SAME
    ``PermissionEngine`` the real run uses. No ``_execute_action`` is called; no
    Run is written; the audit ledger is untouched. Fail closed: if the engine
    would deny, the explanation says denied with the reason.
  * ``replay(engine, run_id, mode)``:
      - ``mode="explain"`` reconstructs what a past run DID from the append-only
        audit ledger (no re-execution, no model) — deterministic, source-of-truth.
      - ``mode="rerun"`` actually executes ``engine.run`` again (a fresh run).
    The two are deliberately distinct: explain = read the ledger; rerun = do it.

The key invariant: an explanation is a *prediction* using the live engine; a
replay-explain is a *record* from the ledger. They are never conflated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import RunStatus


@dataclass
class StepExplanation:
    index: int
    description: str
    tool: str
    args_keys: List[str]
    risk: str
    decision: str  # executed | denied | awaited | skipped
    reason: str


@dataclass
class ExplainResult:
    intent: str
    planner: str
    steps: List[StepExplanation] = field(default_factory=list)
    would_require_approval: bool = False
    would_be_blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "planner": self.planner,
            "would_require_approval": self.would_require_approval,
            "would_be_blocked": self.would_be_blocked,
            "steps": [vars(s) for s in self.steps],
        }


def explain(
    engine: "object",
    request: str,
    *,
    procedure: str = "",
    inputs: Optional[Dict[str, Any]] = None,
) -> ExplainResult:
    """§28/§29 — plan + evaluate permissions, never execute. Fail closed."""
    if getattr(engine.worker, "disabled", False):
        raise RuntimeError(f"worker {engine.worker.name!r} is disabled")
    intent, steps = engine._plan(request, procedure, inputs or {})
    perms = type(engine)._make_perms(engine) if hasattr(engine, "_make_perms") else None
    # Use the engine's real permission plumbing without running tools.
    from .permissions import PermissionEngine, DecompositionGuard

    guard = DecompositionGuard()
    pe = PermissionEngine(engine.worker, guard)
    out = ExplainResult(
        intent=intent,
        planner="deterministic fallback" if not engine.llm.available() else "planner",
    )
    for i, s in enumerate(steps[: engine.__class__.MAX_STEPS]) if hasattr(engine.__class__, "MAX_STEPS") else enumerate(steps):
        tool_name = str(s.get("tool") or "")
        args = dict(s.get("args") or {})
        desc = str(s.get("description") or tool_name or f"step {i}")
        if not tool_name:
            out.steps.append(StepExplanation(i, desc, "", [], "none", "skipped", "reasoning step; no tool"))
            continue
        if not engine.registry.has(tool_name):
            out.steps.append(StepExplanation(i, desc, tool_name, [], "none", "skipped", "tool unavailable"))
            continue
        try:
            tool = engine.registry.get(tool_name)
            validated = tool.validate(args)
        except Exception as exc:  # invalid args -> would be a failure
            out.steps.append(StepExplanation(i, desc, tool_name, list(args), "none", "denied", f"invalid arguments: {exc}"))
            out.would_be_blocked = True
            continue
        decision = pe.evaluate(tool, validated)
        se = StepExplanation(
            i, desc, tool_name, list(validated),
            decision.risk.value if hasattr(decision.risk, "value") else str(decision.risk),
            "awaited" if decision.needs_approval else ("denied" if decision.denied else "executed"),
            decision.reason,
        )
        out.steps.append(se)
        if decision.needs_approval:
            out.would_require_approval = True
        if decision.denied:
            out.would_be_blocked = True
    return out


def replay(engine: "object", run_id: str, mode: str = "explain") -> Dict[str, Any]:
    """§30 — explain (read ledger) vs rerun (execute again)."""
    if mode not in ("explain", "rerun"):
        raise ValueError(f"replay mode must be 'explain' or 'rerun', got {mode!r}")
    if mode == "rerun":
        run = engine.store.get("runs", run_id)
        if run is None:
            raise KeyError(f"no run {run_id!r}")
        req = run.get("intent") or run.get("request") or ""
        result = engine.run(req, procedure=run.get("procedure", ""), trigger="replay")
        return {"mode": "rerun", "run_id": result.run.id, "status": result.status.value}
    # explain: reconstruct from the append-only audit ledger, no model/execute.
    events = list(engine.store.iter_audit(run_id))
    actions = [e for e in events if e.get("event") in ("action.proposed", "action.denied", "action.executed")]
    return {
        "mode": "explain",
        "run_id": run_id,
        "event_count": len(events),
        "actions": [
            {
                "tool": a.get("tool"),
                "summary": a.get("summary"),
                "status": a.get("status"),
                "risk": a.get("risk"),
            }
            for a in actions
        ],
    }
