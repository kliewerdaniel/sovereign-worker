"""§71 — ``/api/v1/sales`` JSON routes (read-only views over the ledger).

These are thin HTTP adapters over ``SalesRepository`` + the sales metrics/checks
modules. They are read-only by design: the autonomous *writes* happen through the
worker tools (gated by the five-tier permission model), not through this API. The
API lets the web UI render the pipeline, a lead, the daily report, and the result
of running the sales verification checks.

Auth + RBAC are enforced by the web handler before this dispatch is reached (same
as every other /api/v1 route). This module only shapes the payload.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .repository import SalesRepository, default_ledger_path
from . import knowledge as sales_knowledge
from . import metrics as sales_metrics
from . import checks as sales_checks  # noqa: F401 (import registers @check hooks)
from ..verify import run_check
from ..config import default_workspace


def _sales_docs_root() -> str:
    import os

    env = os.environ.get("DAILYSALESOS_ROOT", "")
    if env and os.path.isdir(env):
        return env
    cand = os.path.join(default_workspace().root, "sales_knowledge")
    return cand if os.path.isdir(cand) else ""


def dispatch(path: str, qs: Dict[str, List[str]]) -> Tuple[Any, int]:
    """Handle ``/api/v1/sales/*``. ``path`` is the part after ``/sales``."""
    repo = SalesRepository(default_ledger_path())
    try:
        if path in ("", "/"):
            return {
                "service": "sales",
                "routes": [
                    "/api/v1/sales/pipeline",
                    "/api/v1/sales/lead/<id>",
                    "/api/v1/sales/metrics",
                    "/api/v1/sales/icp",
                    "/api/v1/sales/verify",
                ],
            }, 200

        if path == "/pipeline" or path.startswith("/pipeline"):
            stage = (qs.get("stage") or [""])[0]
            if path.endswith("/summary"):
                return repo.pipeline_summary(), 200
            return [l.to_dict() for l in repo.search_leads(stage=stage)], 200

        if path.startswith("/lead/"):
            lead_id = path[len("/lead/"):]
            lead = repo.get_lead(lead_id)
            if not lead:
                return {"error": "lead not found", "lead_id": lead_id}, 404
            out = lead.to_dict()
            out["evidence"] = [e.to_dict() for e in repo.evidence_for(lead_id)]
            out["qualifications"] = [q.to_dict() for q in repo.qualifications_for(lead_id)]
            out["pain_points"] = [p.to_dict() for p in repo.pain_points_for(lead_id)]
            out["drafts"] = [d.to_dict() for d in repo.drafts(lead_id=lead_id)]
            out["stage_history"] = [h.to_dict() for h in repo.stage_history(lead_id)]
            return out, 200

        if path == "/metrics":
            day = (qs.get("day") or [""])[0]
            root = _sales_docs_root()
            targets = sales_knowledge.parse_daily_targets(root) if root else {}
            report = sales_metrics.daily_report(
                repo, targets=targets, targets_source=root or "", day=day
            )
            return report, 200

        if path == "/icp":
            return [icp.to_dict() for icp in repo.active_icp()], 200

        if path == "/verify":
            day = (qs.get("day") or [""])[0]
            ws = default_workspace()
            results = []
            for name in sales_checks.sales_checks:
                try:
                    res = run_check({"check": name, "day": day}, ws.root)
                    results.append(vars(res))
                except Exception as exc:  # report, don't crash
                    results.append({"check": name, "status": "ERROR", "detail": str(exc)})
            return results, 200

        return {"error": "not found", "path": path}, 404
    finally:
        repo.close()
