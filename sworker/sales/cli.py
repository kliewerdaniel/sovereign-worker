"""§71 — ``sworker sales`` command group (local-first sales operating system).

Thin CLI over the sales boundary layer. Every subcommand reads/writes through
``SalesRepository`` (the single writer over the Experiment_Ledger sqlite schema).
Nothing here re-implements an engine; the autonomous loop lives in the worker
YAMLs + the ``DAILY_SALES_RUN`` procedure (invoked via ``sworker run``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from typing import Any, Dict, List

from ..config import default_workspace
from ..verify import run_check
from .repository import SalesRepository, default_ledger_path, SalesError
from . import knowledge as sales_knowledge
from . import metrics as sales_metrics
from . import checks as sales_checks  # noqa: F401  (imports register the @check hooks)
from . import discovery as D, evidence as E, followup as F, qualification as Q, research as R, outreach as O


def _repo() -> SalesRepository:
    return SalesRepository(default_ledger_path())


def _sales_docs_root() -> str:
    """Best-effort path to the DailySalesOS markdown docs (read-only source of truth)."""
    env = os.environ.get("DAILYSALESOS_ROOT", "")
    if env and os.path.isdir(env):
        return env
    # common sibling location; never required (sales_* still works on the ledger alone)
    cand = os.path.join(default_workspace().root, "sales_knowledge")
    return cand if os.path.isdir(cand) else ""


def _jprint(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --------------------------------------------------------------------------- #
# seed — Phase 11: deterministic demo data so the success demo needs no private
# data or live APIs. Idempotent; writes only into the workspace company/ dir.
# --------------------------------------------------------------------------- #
_DEMO_CANDIDATES = [
    {"name": "Acme Robotics", "website": "https://acme-robotics.example",
     "industry": "Manufacturing", "notes": "Cited DailySalesOS ICP: mid-market manufacturer."},
    {"name": "Globex Analytics", "website": "https://globex.example",
     "industry": "SaaS", "notes": "Analytics platform; buying signals in sourcing doc."},
    {"name": "Initech Tools", "website": "https://initech.example",
     "industry": "Manufacturing", "notes": "Tooling supplier expanding sales ops."},
]


def cmd_seed(args) -> int:
    """Materialise a deterministic demo company + candidate list.

    Pure, offline, and reproducible: running it twice yields byte-identical
    files. This is what makes ``sworker sales daily-run`` demonstrable from a
    clean environment without private CRMs or live APIs.
    """
    ws = default_workspace()
    ws.ensure()
    company = ws.company_dir
    os.makedirs(company, exist_ok=True)
    csv_path = os.path.join(company, args.csv_name)
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "website", "industry", "notes"])
        w.writeheader()
        for row in _DEMO_CANDIDATES:
            w.writerow(row)
    # A minimal company-knowledge doc so research has something to read.
    kdoc = os.path.join(company, "acme_robotics.md")
    with open(kdoc, "w") as fh:
        fh.write(
            "# Acme Robotics — Company Knowledge\n\n"
            "Mid-market manufacturer automating shop-floor operations.\n"
            "Pain points: manual lead routing, no single source of pipeline truth.\n"
            "Buying intent: evaluating sales OS after a Q3 expansion.\n"
        )
    print(f"seeded: {csv_path} ({len(_DEMO_CANDIDATES)} candidates)")
    print(f"seeded: {kdoc}")
    print("next:   python -m sworker sales daily-run --source " + args.csv_name)
    return 0


# --------------------------------------------------------------------------- #
# init — materialise worker templates + compile the ICP from the source docs
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    ws = default_workspace()
    ws.ensure()
    tdir = os.path.join(os.path.dirname(__file__), "templates")
    wdir = ws.workers_dir
    pdir = ws.procedures_dir
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(pdir, exist_ok=True)
    copied = []
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        # Worker identities live in workers_dir; procedures (DAILY_*) live in
        # procedures_dir so load_procedure() can resolve them per worker.
        if fn.startswith("DAILY_"):
            dst = os.path.join(pdir, fn)
        else:
            dst = os.path.join(wdir, fn)
        if os.path.exists(dst) and not args.force:
            continue
        shutil.copyfile(os.path.join(tdir, fn), dst)
        copied.append(fn)
    # compile ICP from the DailySalesOS markdown if present
    root = _sales_docs_root()
    compiled = 0
    if root:
        repo = _repo()
        try:
            icps = sales_knowledge.compile_icp(root)
            for icp in icps:
                repo.upsert_icp(icp)
            compiled = len(icps)
        finally:
            repo.close()
    print(f"sales workers written: {copied or 'none (already present; use --force)'}")
    print(f"ICP compiled from {root or '(no DailySalesOS docs found)'}: {compiled} industries")
    print("next: python -m sworker run sales_researcher \"execute DAILY_RESEARCH\"")
    return 0


# --------------------------------------------------------------------------- #
# icp — show / recompile the active ICP
# --------------------------------------------------------------------------- #
def cmd_icp(args) -> int:
    if args.recompile:
        root = _sales_docs_root()
        if not root:
            print("DAILYSALESOS_ROOT not set and no sales_knowledge/ dir; nothing to compile", file=sys.stderr)
            return 1
        repo = _repo()
        try:
            icps = sales_knowledge.compile_icp(root)
            for icp in icps:
                repo.upsert_icp(icp)
        finally:
            repo.close()
        print(f"recompiled {len(icps)} industries")
        return 0
    repo = _repo()
    try:
        rows = [icp.to_dict() for icp in repo.active_icp()]
    finally:
        repo.close()
    _jprint(rows)
    return 0


# --------------------------------------------------------------------------- #
# pipeline — list leads at each stage
# --------------------------------------------------------------------------- #
def cmd_pipeline(args) -> int:
    repo = _repo()
    try:
        if args.summary:
            rows = repo.pipeline_summary()
        else:
            rows = [l for l in repo.search_leads(stage=args.stage or "")]
    finally:
        repo.close()
    _jprint(rows)
    return 0


# --------------------------------------------------------------------------- #
# lead — show one lead, its evidence, qualification, drafts
# --------------------------------------------------------------------------- #
def cmd_lead(args) -> int:
    repo = _repo()
    try:
        lead = repo.get_lead(args.lead_id)
        if not lead:
            print(f"no lead {args.lead_id!r}", file=sys.stderr)
            return 1
        out = lead.to_dict()
        out["evidence"] = [e.to_dict() for e in repo.evidence_for(args.lead_id)]
        out["qualifications"] = [q.to_dict() for q in repo.qualifications_for(args.lead_id)]
        out["pain_points"] = [p.to_dict() for p in repo.pain_points_for(args.lead_id)]
        out["drafts"] = [d.to_dict() for d in repo.drafts(lead_id=args.lead_id)]
    finally:
        repo.close()
    _jprint(out)
    return 0


# --------------------------------------------------------------------------- #
# lead discover / research / qualify — thin operators over the same functions
# the worker procedures (and sales_* tools) call. No logic is duplicated here.
# --------------------------------------------------------------------------- #
def cmd_lead_discover(args) -> int:
    repo = _repo()
    try:
        acc = E.SalesEvidence(repo)
        if args.source == "prospects":
            candidates, source_ref = D.candidates_from_prospects(repo)
        else:
            path = os.path.join("company", args.source)
            if not os.path.isfile(os.path.join(default_workspace().root, path)):
                path = args.source
            candidates, source_ref = D.read_candidates(path)
        res = D.discover(repo, candidates, source_ref=source_ref, source=args.source,
                         limit=args.limit, run_id="cli", evidence=acc)
        print(f"discovered {res['created_count']} new; {res['duplicate_count']} dup; "
              f"{res['rejected_count']} rejected")
    finally:
        repo.close()
    return 0


def cmd_lead_research(args) -> int:
    repo = _repo()
    try:
        acc = E.SalesEvidence(repo)
        root = _sales_docs_root()
        sources = args.sources or ([f"company/{n}" for n in os.listdir(root)]
                                   if root else [])
        res = R.research_lead(repo, args.lead_id, sources, evidence=acc, run_id="cli")
        print(f"{res['evidence_count']} evidence; {len(res['pain_points'])} pain point(s)")
    finally:
        repo.close()
    return 0


def cmd_lead_qualify(args) -> int:
    repo = _repo()
    try:
        q = Q.evaluate(repo, args.lead_id, run_id="cli")
        print(f"{q.lead_id}: score {q.score} ({q.tier.value}) v{q.version}")
    finally:
        repo.close()
    return 0


# --------------------------------------------------------------------------- #
# outreach draft / approve — thin operators over outreach.prepare / approve_draft.
# --------------------------------------------------------------------------- #
def cmd_outreach_draft(args) -> int:
    repo = _repo()
    try:
        offer = sales_knowledge.parse_core_offer(_sales_docs_root())
        seqs = sales_knowledge.parse_followup_sequences(_sales_docs_root())
        res = O.prepare(repo, args.lead_id, sequences=seqs, offer=offer,
                        channel=args.channel, run_id="cli")
        print(f"drafted; requires_approval={res['requires_approval']}; "
              f"draft_id={res['draft'].get('id')}")
    finally:
        repo.close()
    return 0


def cmd_outreach_approve(args) -> int:
    repo = _repo()
    try:
        draft = repo.approve_draft(args.draft_id, args.approved_by)
        print(f"approved {draft.id} by {args.approved_by}")
    except Exception as exc:
        print(f"approve failed: {exc}", file=sys.stderr)
        return 1
    finally:
        repo.close()
    return 0


# --------------------------------------------------------------------------- #
# followups due / schedule — thin operators over followup.due_today / schedule_for_lead
# --------------------------------------------------------------------------- #
def cmd_followups_due(args) -> int:
    repo = _repo()
    try:
        out = F.due_today(repo, on=args.on)
    finally:
        repo.close()
    _jprint(out)
    return 0


def cmd_followups_schedule(args) -> int:
    repo = _repo()
    try:
        seqs = sales_knowledge.parse_followup_sequences(_sales_docs_root())
        res = F.schedule_for_lead(repo, args.lead_id, sequences=seqs, run_id="cli")
        print(f"lead {args.lead_id}: created={res['created']} ({res.get('reason', '')})")
    finally:
        repo.close()
    return 0


# --------------------------------------------------------------------------- #
# metrics — daily report against documented targets
# --------------------------------------------------------------------------- #
def cmd_metrics(args) -> int:
    root = _sales_docs_root()
    targets = sales_knowledge.parse_daily_targets(root) if root else {}
    repo = _repo()
    try:
        report = sales_metrics.daily_report(
            repo, targets=targets, targets_source=root or "", day=args.day
        )
    finally:
        repo.close()
    if args.markdown:
        print(sales_metrics.render_markdown(report))
    else:
        _jprint(report)
    return 0


# --------------------------------------------------------------------------- #
# verify — run the sales verification checks against the ledger
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    ws = default_workspace()
    results = []
    for name in sales_checks.sales_checks:
        spec = {"check": name, "day": args.day}
        try:
            res = run_check(spec, ws.root)
            results.append(vars(res))
        except Exception as exc:  # a failing check is reported, not crashed out
            results.append({"check": name, "status": "ERROR", "detail": str(exc)})
    _jprint(results)
    failed = [r for r in results if r.get("status") not in ("PASS",)]
    print(f"\nsales checks: {len(results)-len(failed)}/{len(results)} passed")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# daily-run — Phase 8 flagship workflow
# --------------------------------------------------------------------------- #
def cmd_daily_run(args) -> int:
    """§8 — the Sales Worker's daily run, orchestrated through the runtime.

    This is NOT a second agent framework. It runs the two worker instances that
    already exist (``sales_researcher`` and ``sales_outreach``) through the same
    ``WorkerEngine`` any other worker would use, then consolidates their reports.

        worker load -> knowledge load -> pipeline inspect -> qualify ->
        recommended actions -> policy respects external egress -> approval gate

    External egress (``sales_record_sent`` / ``sales_bulk_send``) is never
    executed inside the loop; it surfaces as a PENDING APPROVAL the operator
    resolves with ``sworker approve``.
    """
    from ..config import get_worker
    from ..engine import WorkerEngine
    from ..store import WorkerStore  # noqa: F401  (engine owns the store)

    store = WorkerStore(default_workspace().state_dir)
    plan: List[Dict[str, Any]] = []

    # 1) Research pass (discover + research + qualify). No external egress.
    try:
        w = get_worker("sales_researcher")
        eng = WorkerEngine(w, store)
        res = eng.run(
            "execute DAILY_RESEARCH",
            procedure="DAILY_RESEARCH",
            inputs={"source": args.source, "limit": str(args.limit)},
            on_event=_printer,
        )
        plan.append({
            "worker": "sales_researcher",
            "run_id": res.run.id,
            "status": res.status.value,
            "summary": res.summary,
            "ok": "yes" if res.ok else "no",
            "pending": [a["id"] for a in res.pending_approvals],
        })
    except Exception as exc:  # a failed research pass still reports, it does not crash the daily run
        plan.append({"worker": "sales_researcher", "run_id": "", "status": "ERROR",
                     "summary": str(exc), "ok": "no", "pending": []})

    # 2) Outreach pass (draft + schedule + move stage). Holds for approval before send.
    try:
        w = get_worker("sales_outreach")
        eng = WorkerEngine(w, store)
        res = eng.run(
            "execute DAILY_SALES_RUN",
            procedure="DAILY_SALES_RUN",
            inputs={},
            on_event=_printer,
        )
        plan.append({
            "worker": "sales_outreach",
            "run_id": res.run.id,
            "status": res.status.value,
            "summary": res.summary,
            "ok": "yes" if res.ok else "no",
            "pending": [a["id"] for a in res.pending_approvals],
        })
        pending = res.pending_approvals
    except Exception as exc:
        plan.append({"worker": "sales_outreach", "run_id": "", "status": "ERROR",
                     "summary": str(exc), "ok": "no", "pending": []})
        pending = []

    # 3) Consolidated daily report straight from the ledger (re-derivable).
    try:
        repo = _repo()
        parsed = sales_knowledge.parse_daily_targets(_sales_docs_root())
        report = sales_metrics.daily_report(
            repo, targets=parsed.get("targets", {}), targets_source=parsed.get("source_doc", "")
        )
        print()
        print("=" * 64)
        print("DAILY SALES REPORT")
        print("=" * 64)
        print(f"date:          {report['date']}")
        print(f"failed_sales_day: {report['failed_sales_day']}")
        for tkey, v in report["vs_target"].items():
            flag = "MET" if v["met"] else "MISS"
            print(f"  {flag:4} {tkey:22} {v['actual']}/{v['target']}")
        if report["bottlenecks"]:
            print("bottlenecks:")
            for b in report["bottlenecks"]:
                print(f"  - {b}")
        print(f"pending_approvals: {report['pending_approvals']}")
    except Exception as exc:
        print(f"\n(daily report unavailable: {exc})")

    # 4) Show the inspect + approval surface so the operator can finish the loop.
    print()
    print("-" * 64)
    print("PER-RUN SUMMARY")
    for p in plan:
        print(f"  {p['worker']:18} {p['status']:16} ok={p['ok']}")
        if p["run_id"]:
            print(f"    inspect: sworker inspect {p['run_id']}")
            print(f"    replay:  sworker replay {p['run_id']}")
    if pending:
        print()
        print("EXTERNAL EGRESS HELD FOR APPROVAL:")
        for a in pending:
            print(f"  {a['id']}  {a['summary']}  [{a['risk']}]")
        print("  approve with: sworker approve <id>")
        print("  then resume: sworker resume <run_id>")
    return 0


def _printer(event: str, payload: Dict[str, Any]) -> None:
    if event in ("run.started", "run.finished", "plan.created"):
        return
    if event == "step.done":
        print(f"  ✓ {payload.get('step','')[:58]}  ({payload.get('tool')})")
    elif event == "step.failed":
        print(f"  ✗ {payload.get('step','')[:58]}  ERROR: {payload.get('error','')[:70]}")
    elif event == "approval.requested":
        print(f"  ⏳ approval requested: {payload.get('id')} [{payload.get('risk')}] {payload.get('summary')}")


# --------------------------------------------------------------------------- #
# templates — list bundled worker YAMLs + DAILY_SALES_RUN
# --------------------------------------------------------------------------- #
def cmd_templates(args) -> int:
    tdir = os.path.join(os.path.dirname(__file__), "templates")
    rows = sorted(fn for fn in os.listdir(tdir) if fn.endswith((".yaml", ".yml")))
    _jprint(rows)
    return 0


def build_subparser(sub) -> None:
    """Register the ``sales`` group onto the main argparse subparsers."""
    sp = sub.add_parser("sales", help="§71 local-first sales operating system")
    sales_sub = sp.add_subparsers(dest="ssub", required=True)

    p = sales_sub.add_parser("init", help="install sales worker templates + compile ICP")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sales_sub.add_parser("seed", help="§11 materialise deterministic demo candidates + knowledge")
    p.add_argument("--csv-name", default="candidates.csv")
    p.set_defaults(func=cmd_seed)

    p = sales_sub.add_parser("icp", help="show / recompile the active ICP")
    p.add_argument("--recompile", action="store_true")
    p.set_defaults(func=cmd_icp)

    p = sales_sub.add_parser("pipeline", help="list leads by stage")
    p.add_argument("--stage", default="")
    p.add_argument("--summary", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    p = sales_sub.add_parser("lead", help="lead operations: show / discover / research / qualify")
    lead_sub = p.add_subparsers(dest="lsub", required=True)
    lp = lead_sub.add_parser("show", help="full record for one lead")
    lp.add_argument("lead_id")
    lp.set_defaults(func=cmd_lead)
    lp = lead_sub.add_parser("discover", help="ingest candidates from a CSV/prospects")
    lp.add_argument("source", help="candidate CSV under company/ or 'prospects'")
    lp.add_argument("--limit", type=int, default=0)
    lp.set_defaults(func=cmd_lead_discover)
    lp = lead_sub.add_parser("research", help="research one lead from permitted docs")
    lp.add_argument("lead_id")
    lp.add_argument("--sources", nargs="*", default=[])
    lp.set_defaults(func=cmd_lead_research)
    lp = lead_sub.add_parser("qualify", help="score one lead deterministically")
    lp.add_argument("lead_id")
    lp.set_defaults(func=cmd_lead_qualify)

    p = sales_sub.add_parser("outreach", help="outreach operations: draft / approve")
    out_sub = p.add_subparsers(dest="osub", required=True)
    op = out_sub.add_parser("draft", help="draft outreach for one lead (needs approval to send)")
    op.add_argument("lead_id")
    op.add_argument("--channel", default="email")
    op.set_defaults(func=cmd_outreach_draft)
    op = out_sub.add_parser("approve", help="approve a draft for sending")
    op.add_argument("draft_id")
    op.add_argument("--approved-by", default="operator")
    op.set_defaults(func=cmd_outreach_approve)

    p = sales_sub.add_parser("followups", help="follow-up operations: due / schedule")
    fu_sub = p.add_subparsers(dest="fsub", required=True)
    fp = fu_sub.add_parser("due", help="today's follow-ups + SLA-overdue leads")
    fp.add_argument("--on", default="")
    fp.set_defaults(func=cmd_followups_due)
    fp = fu_sub.add_parser("schedule", help="schedule documented next action for a lead")
    fp.add_argument("lead_id")
    fp.set_defaults(func=cmd_followups_schedule)

    p = sales_sub.add_parser("metrics", help="daily sales report vs documented targets")
    p.add_argument("--day", default="")
    p.add_argument("--markdown", action="store_true")
    p.set_defaults(func=cmd_metrics)

    p = sales_sub.add_parser("verify", help="run sales verification checks on the ledger")
    p.add_argument("--day", default="")
    p.set_defaults(func=cmd_verify)

    p = sales_sub.add_parser("templates", help="list bundled worker + procedure templates")
    p.set_defaults(func=cmd_templates)

    p = sales_sub.add_parser("daily-run", help="§8 run the Sales Worker's full daily loop")
    p.add_argument("--source", default="candidates.csv")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_daily_run)
