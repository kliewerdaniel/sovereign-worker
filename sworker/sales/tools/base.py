"""Sales tool implementations.

Every tool is a normal ``sworker.tools.base.Tool`` with a declared ``risk``; the
engine routes it through ``PermissionEngine`` like any other tool, so the worker's
``policy`` decides auto/approve/deny. Tools open their own ``SalesRepository``
against the ledger path they resolve through ``ctx.resolve`` — a worker whose
``fs_roots`` excludes the ledger physically cannot reach it.

Per ``docs/SALES_INTEGRATION.md`` §2, risk assignment is:
    read        lookups, pipeline/evidence/followup inspection, metrics
    reversible  local record creation/update, scoring, stage moves, drafting
    external    approving a draft, recording an external send
    financial   bulk send preparation
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from ...models import RiskLevel
from ...tools.base import Tool, ToolResult, ToolError
from .. import discovery as D, evidence as E, followup as F, knowledge as K, metrics as M
from .. import outreach as O, qualification as Q, research as R
from ..repository import SalesError, SalesRepository, default_ledger_path


def _repo(ctx) -> SalesRepository:
    """Open the ledger through the worker's filesystem boundary."""
    path = getattr(ctx, "sales_ledger", None) or default_ledger_path(ctx.workspace)
    return SalesRepository(ctx.resolve(path))


def _daily_targets(workspace: str) -> Dict[str, Any]:
    parsed = K.parse_daily_targets(K.docs_root(workspace))
    return {
        "targets": parsed.get("targets", {}),
        "source_doc": parsed.get("source_doc", ""),
        "found": parsed.get("found", False),
    }


def _company_docs(ctx) -> List[str]:
    """All readable markdown/CSV source files under the worker's company/ root."""
    import os

    root = ctx.resolve("company", must_exist=True)
    out = []
    for name in sorted(os.listdir(root)):
        if name.lower().endswith((".md", ".csv", ".txt", ".json")):
            out.append(f"company/{name}")
    return out


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------


class SalesPipelineListTool(Tool):
    name = "sales_pipeline_list"
    description = "List leads, optionally filtered by stage/industry/min-score. Read-only."
    risk = RiskLevel.READ
    input_schema = {
        "type": "object",
        "properties": {
            "stage": {"type": "string", "default": ""},
            "industry": {"type": "string", "default": ""},
            "min_score": {"type": "number", "default": -1},
            "query": {"type": "string", "default": ""},
            "limit": {"type": "integer", "default": 50},
        },
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        rows = repo.search_leads(
            stage=args.get("stage", ""),
            industry=args.get("industry", ""),
            min_score=float(args.get("min_score", -1)),
            query=args.get("query", ""),
            limit=int(args.get("limit", 50)),
        )
        return ToolResult(ok=True, output=f"{len(rows)} lead(s)", data={"leads": rows})


class SalesEvidenceExplainTool(Tool):
    name = "sales_evidence_explain"
    description = "Explain WHY a lead scored as it did, from stored evidence only. Read-only."
    risk = RiskLevel.READ
    input_schema = {
        "type": "object",
        "properties": {"lead_id": {"type": "string"}},
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        acc = E.SalesEvidence(repo)
        return ToolResult(ok=True, output="evidence for " + args["lead_id"], data=acc.explain(args["lead_id"]))


class SalesLeadDetailTool(Tool):
    name = "sales_lead_detail"
    description = "Full detail of one lead (company, contacts, score history, evidence, follow-ups). Read-only."
    risk = RiskLevel.READ
    input_schema = {
        "type": "object",
        "properties": {"lead_id": {"type": "string"}},
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        return ToolResult(ok=True, output="lead detail " + args["lead_id"], data=repo.lead_detail(args["lead_id"]))


class SalesStaleLeadsTool(Tool):
    name = "sales_stale_leads"
    description = "Leads past the documented max duration for their stage (pipeline hygiene). Read-only."
    risk = RiskLevel.READ
    input_schema = {"type": "object", "properties": {"as_of": {"type": "number", "default": 0}}}

    def run(self, ctx, args):
        repo = _repo(ctx)
        as_of = args.get("as_of") or None
        rows = repo.stale_leads(as_of=as_of)
        return ToolResult(ok=True, output=f"{len(rows)} stale lead(s)", data={"stale": rows, "overdue_count": len(rows)})


class SalesPipelineSummaryTool(Tool):
    name = "sales_pipeline_summary"
    description = "Count of leads per stage with avg score and pipeline value. Read-only."
    risk = RiskLevel.READ
    input_schema = {"type": "object", "properties": {}}

    def run(self, ctx, args):
        repo = _repo(ctx)
        return ToolResult(ok=True, output="pipeline summary", data={"summary": repo.pipeline_summary()})


class SalesFollowupDueTool(Tool):
    name = "sales_followup_due"
    description = "Today's follow-ups, tasks and SLA-overdue leads. Read-only."
    risk = RiskLevel.READ
    input_schema = {"type": "object", "properties": {"on": {"type": "string", "default": ""}}}

    def run(self, ctx, args):
        repo = _repo(ctx)
        return ToolResult(ok=True, output="due items", data=F.due_today(repo, on=args.get("on", "")))


class SalesMetricsTool(Tool):
    name = "sales_metrics"
    description = "Daily activity counts + vs-target report from the ledger. Read-only."
    risk = RiskLevel.READ
    input_schema = {
        "type": "object",
        "properties": {"day": {"type": "string", "default": ""}, "markdown": {"type": "boolean", "default": False}},
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        tgt = _daily_targets(ctx.workspace)
        report = M.daily_report(
            repo, targets=tgt["targets"], targets_source=tgt["source_doc"], day=args.get("day", "")
        )
        out = M.render_markdown(report) if args.get("markdown") else ""
        artifacts = []
        if args.get("markdown"):
            p = ctx.resolve("daily_report.md", must_exist=False)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(out)
            artifacts.append(p)
        return ToolResult(ok=True, output=out or f"report for {report['date']}", data=report, artifacts=artifacts)


# ---------------------------------------------------------------------------
# reversible tools
# ---------------------------------------------------------------------------


class SalesDiscoverTool(Tool):
    name = "sales_discover"
    description = "Ingest candidate companies from a permitted file (CSV/JSON) or the prospects table, dedupe, create leads and record provenance evidence."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "candidate file path inside fs_roots, or 'prospects'"},
            "limit": {"type": "integer", "default": 0},
        },
        "required": ["source"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        acc = E.SalesEvidence(repo)
        source = args["source"]
        if source == "prospects":
            candidates, source_ref = D.candidates_from_prospects(repo)
        else:
            # Resolve the candidate file. The worker's fs boundary is `company/`,
            # so a bare name like "candidates.csv" is looked up under company/
            # first, then at the given path. Fail closed on an unreadable file.
            path = None
            for cand in (source, os.path.join("company", source)):
                try:
                    path = ctx.resolve(cand, must_exist=True)
                    break
                except ToolError:
                    continue
            if path is None:
                return ToolResult(
                    ok=False,
                    error=f"candidate source {source!r} not found inside the worker's filesystem boundary",
                )
            candidates, source_ref = D.read_candidates(path)
        result = D.discover(
            repo, candidates, source_ref=source_ref, source=source,
            limit=int(args.get("limit", 0)), run_id=ctx.run_id, evidence=acc,
        )
        return ToolResult(
            ok=True,
            output=f"discovered {result['created_count']} new lead(s); "
            f"{result['duplicate_count']} duplicate, {result['rejected_count']} rejected",
            data=result,
            evidence=[{"source_ref": source_ref, "excerpt": f"batch of {len(candidates)} candidates"}],
        )


class SalesResearchTool(Tool):
    name = "sales_research"
    description = "Research a lead from permitted source files: record ICP/size/hiring/contact evidence and derive pain points from rubric signal phrases. Every claim cites its source."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {
            "lead_id": {"type": "string", "description": "lead id, or 'all' to process every un-researched lead"},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "explicit source files; if omitted, all docs under company/ are used"},
        },
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        acc = E.SalesEvidence(repo)
        lead_id = args.get("lead_id") or ""
        sources = args.get("sources") or []
        if not sources:
            # No explicit sources: research every permitted markdown doc under company/.
            sources = _company_docs(ctx)
        if lead_id == "all" or lead_id == "":
            # Research every lead that has no qualifying evidence yet.
            leads = repo.search_leads()
            if not leads:
                return ToolResult(ok=True, output="no leads to research", data={"researched": []})
            total_ev = total_pp = 0
            done = []
            for lead in leads:
                lid = lead["id"]
                if repo.evidence_for(lid):
                    continue  # already researched
                paths = [ctx.resolve(s, must_exist=True) for s in sources if ctx.resolve(s, must_exist=False)]
                if not paths:
                    continue
                res = R.research_lead(repo, lid, paths, evidence=acc, run_id=ctx.run_id)
                total_ev += res["evidence_count"]
                total_pp += len(res["pain_points"])
                done.append(lid)
            return ToolResult(
                ok=True, output=f"researched {len(done)} lead(s): {total_ev} evidence, {total_pp} pain point(s)",
                data={"researched": done, "evidence_count": total_ev, "pain_points": total_pp},
            )
        paths = [ctx.resolve(s, must_exist=True) for s in sources]
        result = R.research_lead(repo, lead_id, paths, evidence=acc, run_id=ctx.run_id)
        return ToolResult(
            ok=True,
            output=f"{result['evidence_count']} evidence, {len(result['pain_points'])} pain point(s)",
            data=result,
            evidence=[{"source_ref": s, "excerpt": "research source read"} for s in result["sources_read"]],
        )


class SalesQualifyTool(Tool):
    name = "sales_qualify"
    description = "Score a lead deterministically from stored evidence (append-only version). Fails if the lead has no evidence."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {"lead_id": {"type": "string"}, "use_model": {"type": "boolean", "default": False}},
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        inference = None
        if args.get("use_model"):
            from ...inference import Inference

            inference = Inference()
        lead_id = args.get("lead_id") or ""
        if lead_id == "all" or lead_id == "":
            # Qualify every lead that has evidence but no qualification yet.
            leads = repo.search_leads()
            if not leads:
                return ToolResult(ok=True, output="no leads to qualify", data={"qualified": []})
            done = []
            for lead in leads:
                lid = lead["id"]
                if repo.latest_qualification(lid):
                    continue
                try:
                    Q.evaluate(repo, lid, run_id=ctx.run_id, inference=inference)
                except Q.InsufficientEvidence:
                    continue
                done.append(lid)
            return ToolResult(ok=True, output=f"qualified {len(done)} lead(s)", data={"qualified": done})
        try:
            qual = Q.evaluate(repo, lead_id, run_id=ctx.run_id, inference=inference)
        except Q.InsufficientEvidence as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(
            ok=True, output=f"{qual.lead_id}: score {qual.score} ({qual.tier.value}) v{qual.version}", data=qual.to_dict()
        )


class SalesMoveStageTool(Tool):
    name = "sales_move_stage"
    description = "Advance a lead to a documented-adjacent pipeline stage. Illegal transitions are refused."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {
            "lead_id": {"type": "string"},
            "to_stage": {"type": "string"},
            "reason": {"type": "string", "default": ""},
        },
        "required": ["lead_id", "to_stage"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        try:
            mv = repo.move_stage(
                args["lead_id"], args["to_stage"], reason=args.get("reason", ""),
                run_id=ctx.run_id, worker=ctx.worker,
            )
        except SalesError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, output=f"{mv['from']} -> {mv['to']}", data=mv)


class SalesDraftOutreachTool(Tool):
    name = "sales_draft_outreach"
    description = "Draft a personalised outreach message from stored facts + the offer text + follow-up sequence. Never sends; requires approval to send."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {
            "lead_id": {"type": "string"},
            "channel": {"type": "string", "default": "email"},
            "contact_id": {"type": "string", "default": ""},
            "use_model": {"type": "boolean", "default": False},
        },
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        offer = K.parse_core_offer(K.docs_root(ctx.workspace))
        sequences = K.parse_followup_sequences(K.docs_root(ctx.workspace))
        inference = None
        if args.get("use_model"):
            from ...inference import Inference

            inference = Inference()
        try:
            result = O.prepare(
                repo, args["lead_id"], sequences=sequences, offer=offer,
                channel=args.get("channel", "email"), contact_id=args.get("contact_id", ""),
                run_id=ctx.run_id, inference=inference,
            )
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(
            ok=True,
            output=f"drafted {result['draft']['channel']} for {args['lead_id']}; "
            f"model_rewrite={'used' if result['model']['used'] else 'not used'}",
            data=result,
        )


class SalesScheduleFollowupTool(Tool):
    name = "sales_schedule_followup"
    description = "Schedule the documented next action for a lead per its stage's follow-up rule. Idempotent."
    risk = RiskLevel.REVERSIBLE
    input_schema = {
        "type": "object",
        "properties": {"lead_id": {"type": "string"}},
        "required": ["lead_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        sequences = K.parse_followup_sequences(K.docs_root(ctx.workspace))
        result = F.schedule_for_lead(repo, args["lead_id"], sequences=sequences, run_id=ctx.run_id)
        return ToolResult(ok=True, output=str(result["reason"]), data=result)


# ---------------------------------------------------------------------------
# external tools
# ---------------------------------------------------------------------------


class SalesApproveDraftTool(Tool):
    name = "sales_approve_draft"
    description = "Mark an outreach draft as approved (external send path requires this first). Records the approver."
    risk = RiskLevel.EXTERNAL
    input_schema = {
        "type": "object",
        "properties": {"draft_id": {"type": "string"}, "approved_by": {"type": "string"}},
        "required": ["draft_id", "approved_by"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        try:
            draft = repo.approve_draft(args["draft_id"], args["approved_by"])
        except SalesError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, output=f"approved {draft.id}", data=draft.to_dict())


class SalesRecordSentTool(Tool):
    name = "sales_record_sent"
    description = "Record that an APPROVED draft was actually delivered. Refuses unapproved drafts. Feeds the DailySalesOS experiment tables."
    risk = RiskLevel.EXTERNAL
    input_schema = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string"},
            "receipt": {"type": "string", "default": ""},
            "experiment_id": {"type": "string", "default": ""},
        },
        "required": ["draft_id"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        try:
            rec = repo.record_sent(
                args["draft_id"], receipt=args.get("receipt", ""), experiment_id=args.get("experiment_id", "")
            )
        except SalesError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, output="recorded send", data=rec)


# ---------------------------------------------------------------------------
# financial tools
# ---------------------------------------------------------------------------


class SalesBulkSendTool(Tool):
    name = "sales_bulk_send"
    description = "Approve-and-send a batch of approved drafts in one action. Financial risk: external egress at volume."
    risk = RiskLevel.FINANCIAL
    input_schema = {
        "type": "object",
        "properties": {
            "experiment_id": {"type": "string", "default": ""},
            "approved_by": {"type": "string"},
            "max": {"type": "integer", "default": 0},
        },
        "required": ["approved_by"],
    }

    def run(self, ctx, args):
        repo = _repo(ctx)
        approved = repo.drafts(state="approved")
        if args.get("max"):
            approved = approved[: int(args["max"])]
        sent, failed = [], []
        for d in approved:
            try:
                repo.record_sent(d.id, experiment_id=args.get("experiment_id", ""), receipt="bulk")
                sent.append(d.id)
            except SalesError as exc:
                failed.append({"draft_id": d.id, "error": str(exc)})
        return ToolResult(
            ok=True, output=f"sent {len(sent)}; {len(failed)} skipped", data={"sent": sent, "failed": failed}
        )


# ---------------------------------------------------------------------------
# registry export
# ---------------------------------------------------------------------------


SALES_TOOLS = [
    SalesPipelineListTool(),
    SalesEvidenceExplainTool(),
    SalesLeadDetailTool(),
    SalesStaleLeadsTool(),
    SalesPipelineSummaryTool(),
    SalesFollowupDueTool(),
    SalesMetricsTool(),
    SalesDiscoverTool(),
    SalesResearchTool(),
    SalesQualifyTool(),
    SalesMoveStageTool(),
    SalesDraftOutreachTool(),
    SalesScheduleFollowupTool(),
    SalesApproveDraftTool(),
    SalesRecordSentTool(),
    SalesBulkSendTool(),
]
