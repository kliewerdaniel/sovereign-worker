#!/usr/bin/env python3
"""sworker — the Sovereign AI Worker Platform command line.

Local-first. No cloud. Every action is recorded and reconstructable.

    python -m sworker <command> [args]

Commands:
    init                scaffold a workspace (.sworker/) with an example worker
    workers             list configured workers
    show <worker>       print a worker's identity + policy
    run <worker> "req"  execute a request end-to-end (auto-approve by policy)
    approve <appr_id>   approve a pending approval (or: deny)
    deny <appr_id>      reject a pending approval
    resume <run_id>     continue a run awaiting approval after a decision
    runs [worker]       list runs
    run <id>            show one run's events + evidence + artifacts (id is numeric-ish)
    verify <run>        run any declared verification checks for a run
    learn <run> <name>  capture a completed run as a reusable procedure
    proc [worker]       list procedures
    sched add <w> <p> <cron>   schedule a procedure on a worker
    sched [worker]      list schedules
    sched off <id>      disable a schedule
    audit <run_id>      replay the raw append-only event log for a run

Examples:
    python -m sworker init
    python -m sworker run analyst "What were total Q2 sales?"
    python -m sworker approve appr_3Kf
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List

from . import __version__
from .config import WorkerConfig, default_workspace, get_worker, list_workers, load_worker
from .store import WorkerStore
from .inference import Inference, NullInference
from .engine import WorkerEngine
from .approvals import ApprovalManager, ApprovalError
from .procedures import learn_from_run, list_procedures, load_procedure, save_procedure
from . import scheduler as sched_mod
from . import web as web_mod
from .auth import AuthProvider
from .rbac import RBAC
from .policy import PolicyStore
from .secrets import SecretStore, EncryptionUnavailable, redact_static


def _store() -> WorkerStore:
    ws = default_workspace()
    return WorkerStore(ws.state_dir)


def _ws_label() -> str:
    try:
        return os.path.basename(default_workspace().root)
    except Exception:
        return ""


def _jprint(obj: Any) -> None:
    """§38 — emit a structured JSON view of command output."""
    print(json.dumps(obj, indent=2, default=str))


def _wants_json(args) -> bool:
    return bool(getattr(args, "json", False))


def _engine(worker: WorkerConfig) -> WorkerEngine:
    try:
        llm = Inference.from_env()
    except RuntimeError:
        llm = NullInference()
        print("[inference] no local model at SWORKER_LLM_URL; using deterministic fallback", file=sys.stderr)
    return WorkerEngine(worker, _store(), inference=llm)


def _fmt_run(rec: Dict[str, Any]) -> str:
    return (
        f"  #{rec['seq']:>3}  {rec['id']}  {rec['status']:<20} "
        f"ev={rec.get('evidence_count',0)} art={rec.get('artifact_count',0) or len(rec.get('artifact_ids',[]))}  {rec.get('summary','')[:60]}"
    )


def cmd_init(args) -> int:
    ws = default_workspace()
    ws.ensure()
    wf = os.path.join(ws.workers_dir, "analyst.yaml")
    if not os.path.exists(wf):
        open(wf, "w").write(
            "name: analyst\n"
            "role: local business analyst\n"
            "instructions: |\n"
            "  Read company data under company/, answer questions with computed evidence,\n"
            "  and write reports to artifacts/ when asked.\n"
            "tools: [fs.list, fs.read, fs.write, data.query, data.inspect, knowledge.search]\n"
            "policy:\n"
            "  read: auto\n"
            "  reversible: auto\n"
            "  external: approve\n"
            "  financial: approve\n"
            "  destructive: approve\n"
        )
    os.makedirs(os.path.join(ws.root, "company"), exist_ok=True)
    open(os.path.join(ws.root, "company", "example.csv"), "w").write(
        "region,quarter,revenue\nnorth,Q1,120\nnorth,Q2,150\nsouth,Q1,90\nsouth,Q2,140\n"
    )
    print(f"workspace initialised at {ws.root}")
    print(f"  worker 'analyst' created. Data goes in {os.path.join(ws.root,'company')}")
    print("  try: python -m sworker run analyst \"What were total Q2 revenue?\"")
    return 0


def cmd_onboard(args) -> int:
    """§46 — guided first-run onboarding (fail-closed: never clobbers, never leaks secrets)."""
    ws = default_workspace()
    ws.ensure()
    # 1) default workspace + analyst worker (idempotent: skips existing files)
    cmd_init(None)
    # 2) create an admin user if none exists (fail closed: refuse if users present)
    store = WorkerStore(ws.state_dir)
    existing = store.find("users")
    if existing:
        print(f"\nusers already exist ({len(existing)}); leaving auth as-is.")
    else:
        uname = args.username or "admin"
        pw = args.password or getpass.getpass(f"set password for '{uname}': ") or "changeme"
        auth = AuthProvider(store)
        auth.create_user(uname, pw, role="admin")
        print(f"\ncreated admin user '{uname}' (role=admin).")
        print("  web UI: python -m sworker web --port 8777  (login at http://127.0.0.1:8777/login)")
    print("\nnext steps:")
    print("  run a worker:    python -m sworker run analyst \"What were total Q2 revenue?\"")
    print("  list workers:    python -m sworker workers")
    print("  start the UI:    python -m sworker web --port 8777")
    return 0


def cmd_workers(args) -> int:
    workers = list_workers()
    if _wants_json(args):
        _jprint([{"name": w.name, "role": w.role, "tools": list(w.tools),
                  "policy": w.policy} for w in workers])
        return 0
    for w in workers:
        print(f"{w.name:<16} {w.role:<28} tools={len(w.tools)} policy={w.policy}")
    return 0


def cmd_show(args) -> int:
    w = get_worker(args.worker)
    if _wants_json(args):
        _jprint({"name": w.name, "role": w.role, "tools": list(w.tools),
                 "policy": w.policy, "workspace": w.workspace})
        return 0
    print(f"name: {w.name}")
    print(f"role: {w.role}")
    print(f"tools: {', '.join(w.tools) or '(all)'}")
    print("policy:")
    for k, v in w.policy.items():
        print(f"  {k:<12} {v}")
    print(f"workspace: {w.workspace}")
    return 0


def cmd_run(args) -> int:
    worker = get_worker(args.worker)
    eng = _engine(worker)
    store = eng.store
    res = eng.run(args.request, inputs=_inputs(args), on_event=_printer)
    print()
    print("=" * 64)
    print(f"RUN #{res.run.seq}  {res.status.value}")
    print("-" * 64)
    print(res.summary)
    for art in res.artifacts:
        print(f"  artifact: {art.path}")
    if res.run.degradations:
        print("  DEGRADATIONS (capability reduced; run kept working):")
        for d in res.run.degradations:
            print(f"    ! {d}")
    if res.pending_approvals:
        print("  PENDING APPROVALS:")
        for a in res.pending_approvals:
            print(f"    {a['id']}  {a['summary']}  [{a['risk']}]")
        print(f"  -> review with: python -m sworker show-approval <id>")
    print(f"  replay audit: python -m sworker audit {res.run.id}")
    return 0 if res.ok else 1


def _printer(event: str, payload: Dict[str, Any]) -> None:
    if event in ("run.started", "run.finished", "plan.created"):
        return
    if event == "step.done":
        print(f"  ✓ {payload.get('step','')[:60]}  ({payload.get('tool')})")
    elif event == "step.failed":
        print(f"  ✗ {payload.get('step','')[:60]}  ERROR: {payload.get('error','')[:80]}")
    elif event == "step.blocked":
        print(f"  ⊘ blocked: {payload.get('step','')[:60]}  ({payload.get('reason','')[:60]})")
    elif event == "approval.requested":
        print(f"  ⏳ approval requested: {payload.get('id')} [{payload.get('risk')}] {payload.get('summary')}")


def _inputs(args) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kv in args.input or []:
        if "=" in kv:
            k, _, v = kv.partition("=")
            out[k] = v
    return out


def cmd_approve(args) -> int:
    return _decide(args.appr_id, True, args.note, args.by, args.role)


def cmd_deny(args) -> int:
    return _decide(args.appr_id, False, args.note, args.by, args.role)


def _decide(appr_id: str, approved: bool, note: str, by: str = "cli", role: str = "") -> int:
    store = _store()
    mgr = ApprovalManager(store)
    try:
        rec = mgr.decide(appr_id, approved=approved, by=by, role=role, note=note or "")
    except KeyError as exc:
        print(f"no pending approval {appr_id!r}", file=sys.stderr)
        return 1
    except ApprovalError as exc:
        print(f"vote not recorded: {exc}", file=sys.stderr)
        return 1
    verb = "APPROVED" if approved else "REJECTED"
    # quorum may mean a single vote does not yet settle the approval
    if rec["state"] == "PENDING":
        need = rec.get("quorum", 1)
        have = sum(1 for v in rec.get("votes", []) if v["state"] == "APPROVED")
        print(f"{verb} recorded for {appr_id} ({rec['summary']})")
        print(f"  quorum not yet met: {have}/{need} approvals. run stays PENDING.")
        return 0
    print(f"{verb} {appr_id} ({rec['summary']})")
    print(f"  resume the run with: python -m sworker resume {rec['run_id']}")
    return 0


def cmd_resume(args) -> int:
    store = _store()
    run = store.get("runs", args.run_id)
    if not run:
        print(f"no run {args.run_id!r}", file=sys.stderr)
        return 1
    worker = get_worker(run["worker"])
    eng = _engine(worker)
    # reuse engine but only for resume
    res = eng.run("", resume_run_id=args.run_id, on_event=_printer)
    print()
    print(f"RUN {args.run_id}  -> {res.status.value}")
    print(res.summary)
    return 0 if res.ok else 1


def cmd_runs(args) -> int:
    store = _store()
    rows = store.find("runs", order="seq", desc=True)
    if args.worker:
        rows = [r for r in rows if r["worker"] == args.worker]
    if _wants_json(args):
        _jprint(rows[: args.limit])
        return 0
    if not rows:
        print("(no runs)")
        return 0
    for r in rows[: args.limit]:
        print(_fmt_run(r))
    return 0


def cmd_run_show(args) -> int:
    store = _store()
    run = store.get("runs", args.run_id)
    if not run:
        # allow lookup by seq
        for r in store.find("runs", order="seq"):
            if str(r["seq"]) == args.run_id:
                run = r
                break
    if not run:
        if _wants_json(args):
            _jprint({"error": f"no run {args.run_id!r}"})
            return 1
        print(f"no run {args.run_id!r}", file=sys.stderr)
        return 1
    if _wants_json(args):
        out = {
            "seq": run["seq"],
            "id": run["id"],
            "worker": run["worker"],
            "status": run["status"],
            "intent": run.get("intent", ""),
            "summary": run["summary"],
            "steps": store.find("steps", run_id=run["id"], order="idx"),
            "evidence": store.find("evidence", run_id=run["id"], order="created"),
            "artifacts": store.find("artifacts", run_id=run["id"], order="created"),
            "pending_approvals": ApprovalManager(store).pending(run["id"]),
        }
        _jprint(out)
        return 0
    print(f"RUN #{run['seq']}  {run['id']}")
    print(f"worker: {run['worker']}  status: {run['status']}")
    print(f"intent: {run.get('intent','')}")
    print(f"summary: {run['summary']}")
    print("\nsteps:")
    for s in store.find("steps", run_id=run["id"], order="idx"):
        print(f"  [{s['status']:<18}] {s.get('description','')[:60]}")
    print("\nevidence:")
    for e in store.find("evidence", run_id=run["id"], order="created"):
        print(f"  ({e['provenance']}) {e['summary'][:80]}")
    print("\nartifacts:")
    for a in store.find("artifacts", run_id=run["id"], order="created"):
        print(f"  {a['path']}  ({a.get('bytes',0)} bytes)")
    pending = ApprovalManager(store).pending(run["id"])
    if pending:
        print("\npending approvals:")
        for a in pending:
            print(f"  {a['id']}  {a['summary']}  [{a['risk']}]")
    return 0


def cmd_why(args) -> int:
    """§65 — explain why a run (or the workspace) is/was blocked."""
    from .block_explainer import BlockExplainer, explain_blocked
    from .store import WorkerStore
    from .config import default_workspace, Workspace

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    store = WorkerStore(ws.state_dir)
    if getattr(args, "workspace", False):
        out = BlockExplainer(store).explain_workspace()
    else:
        out = explain_blocked(store, args.run_id)
    if _wants_json(args):
        _jprint(out)
        return 0
    print(f"run {out['run_id'] if 'run_id' in out else '<workspace>'}: "
          f"{out.get('status') or 'n/a'}")
    print(out["summary"])
    if out["reasons"]:
        print("--- reasons ---")
        for r in out["reasons"]:
            print(f"  [{r['severity']:<8}] {r['source']}/{r['kind']}: {r['reason']}")
            if r.get("mitigation"):
                print(f"      fix: {r['mitigation']}")
    return 0


def cmd_maturity(args) -> int:
    """§70 — score the deployment's hardening posture from real state."""
    from .maturity import MaturityModel
    from .maturity import STANDARD as _STD

    store = _store()
    ws = _ws_label()
    rep = MaturityModel(store, ws).assess()
    if _wants_json(args):
        _jprint(rep.to_dict())
        return 0
    print(f"maturity: {rep.level.upper()}  (floor of {len(rep.dimensions)} dimensions, mean {rep.mean:.2f})")
    for d in rep.dimensions:
        flag = "OK " if d.tier >= _STD else "!! "
        print(f"  {flag}{d.label:<38} {d.tier_name}")
    print(f"\n{rep.summary}")
    return 0


def cmd_status(args) -> int:
    """§66 — compose every hardening control into one fail-closed verdict."""
    from .system_status import SystemStatus

    out = SystemStatus(_store()).compose()
    if _wants_json(args):
        _jprint(out)
        return 0
    print(f"system verdict: {out['verdict'].upper()}")
    for c in out["controls"]:
        print(f"  [{c['severity']:<8}] {c['name']}: {c['status']}")
    return 0


def cmd_benchmark(args) -> int:
    """§58/§59 — run real deterministic benchmarks (no LLM) and assert thresholds."""
    from .config import Workspace, get_worker
    from .benchmark import run_benchmarks

    iterations = max(1, int(getattr(args, "iterations", 3)))

    def make_engine() -> "WorkerEngine":  # type: ignore[name-defined]
        import os

        from .config import default_workspace
        from .tools import build_registry

        home = os.environ.get("SWORKER_HOME") or default_workspace()
        ws = Workspace(str(home))
        worker = get_worker(getattr(args, "worker", "acme-analyst"), ws)
        store = WorkerStore(ws.state_dir)
        return WorkerEngine(worker, store, inference=NullInference(), registry=build_registry())

    report = run_benchmarks(make_engine, iterations=iterations, fail_on_regression=not getattr(args, "no_fail", False))
    if _wants_json(args):
        _jprint(report.to_dict())
        return 0
    print(f"benchmark: {len(report.cases)} case(s), {iterations} iteration(s) each")
    for c in report.cases:
        print(f"  {c.name:<20} p50={c.p50_ms:7.2f}ms  p95={c.p95_ms:7.2f}ms  "
              f"status={c.status}  derived={c.derived_total}")
    return 0


def cmd_audit(args) -> int:
    store = _store()
    recs = []
    for rec in store.iter_audit(args.run_id):
        if args.run_id and rec.get("payload", {}).get("run_id") != args.run_id and rec.get("id") != args.run_id:
            continue
        recs.append(rec)
    if _wants_json(args):
        _jprint(recs)
        return 0
    for rec in recs:
        print(f"{rec['ts']:.3f}  {rec['event']:<22} {rec['table']:<13} {rec['id']}")
    return 0


def cmd_learn(args) -> int:
    store = _store()
    body = learn_from_run(store, args.run_id, args.name)
    worker = None
    # best-effort: find the worker that owns the run
    run = store.get("runs", args.run_id)
    if run:
        try:
            worker = get_worker(run["worker"])
        except Exception:
            worker = None
    if worker:
        path = save_procedure(worker, args.name, body)
    else:
        path = os.path.join(default_workspace().procedures_dir, f"{args.name}.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(body)
    print(f"procedure saved: {path}")
    return 0


def cmd_proc(args) -> int:
    store = _store()
    if args.worker:
        worker = get_worker(args.worker)
        procs = list_procedures(worker)
    else:
        procs = []
        for w in list_workers():
            procs.extend(list_procedures(w))
    if _wants_json(args):
        _jprint(procs)
        return 0
    if not procs:
        print("(no procedures)")
        return 0
    for p in procs:
        print(f"  {p.get('name'):<20} learned_from={p.get('learned_from_run','-')}  {p.get('intent','')[:50]}")
    return 0


def cmd_sched(args) -> int:
    store = _store()
    rbac = RBAC()
    # who is acting? CLI runs as an authenticated principal; fall back to a
    # configured user (SWORKER_USER) or "cli" (fail-closed: unknown role denies).
    actor = os.environ.get("SWORKER_USER", "cli")
    ap = _auth()
    acting_user = ap.get_user(actor)
    role = acting_user.role if acting_user else "viewer"
    if args.sub in ("add", "off") and not rbac.authorize(role, "schedule:manage"):
        print(f"error: {actor} (role={role}) not permitted to manage schedules", file=sys.stderr)
        return 3
    if args.sub == "add":
        sched_mod.add_schedule(store, args.worker, args.procedure, args.cron, created_by=actor)
        print(f"scheduled {args.procedure} on {args.worker} at '{args.cron}' (by {actor})")
        return 0
    if args.sub == "off":
        sched_mod.set_enabled(store, args.id, False, by=actor)
        print(f"disabled schedule {args.id} (by {actor})")
        return 0
    # list
    rows = sched_mod.list_schedules(store, getattr(args, "worker", "") or "")
    if _wants_json(args):
        _jprint(rows)
        return 0
    if not rows:
        print("(no schedules)")
        return 0
    now_s = time.time()
    for s in rows:
        due = "DUE" if (s["enabled"] and s["next_run"] and s["next_run"] <= now_s) else ""
        nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["next_run"])) if s.get("next_run") else "-"
        print(f"  {s['id']}  {s['worker']}/{s['procedure']}  {s['cron']:<14} next={nxt} {due} {'[off]' if not s['enabled'] else ''} by={s.get('created_by','')}")
    return 0


def cmd_knowledge(args) -> int:
    """§17 knowledge-index management: status / incremental / rebuild."""
    from . import knowledge as K
    from .config import default_workspace, Workspace

    if args.home:
        ws = Workspace(os.path.abspath(args.home))
    else:
        ws = default_workspace()
    ws.ensure()
    roots = [ws.company_dir] + list(args.root or [])
    atlas_dir = ws.atlas_dir

    if args.sub == "status":
        st = K.atlas_index_status(atlas_dir)
        if _wants_json(args):
            _jprint(st)
            return 0
        if not st["compiled"]:
            print(f"knowledge index: NOT compiled ({st.get('reason', 'unknown')})")
            return 0
        print(f"knowledge index @ {atlas_dir}")
        print(f"  sources={st['sources']} claims={st['claims']} "
              f"entities={st['entities']} contradictions={st['contradictions']}")
        print(f"  fingerprint={st['fingerprint'][:16]}")
        print(f"  changelog_entries={st['changelog_entries']}")
        if st["stale_count"]:
            print(f"  STALE sources ({st['stale_count']}) — recompile to refresh claims:")
            for s in st["stale"]:
                print(f"    - {s.get('path')} (recorded {s.get('recorded_checksum')} -> live {s.get('live_checksum')})")
        if st["missing_count"]:
            print(f"  MISSING sources ({st['missing_count']}) — file gone since compile:")
            for s in st["missing_sources"]:
                print(f"    - {s.get('title')} ({s.get('path')})")
        if not st["stale_count"] and not st["missing_count"]:
            print("  index is current (no stale or missing sources)")
        return 0

    if args.sub == "compile":
        rep = K.incremental_compile(roots, atlas_dir)
        if not rep.get("ok"):
            print(f"compile failed: {rep.get('reason')} ({rep.get('error', '')})", file=sys.stderr)
            return 1
        print(f"incremental compile: {rep.get('sources', 0)} sources "
              f"({rep.get('markdown_files', 0)} md + {rep.get('adapter_files', 0)} adapter), "
              f"{rep.get('stats', {}).get('claims', 0)} claims "
              f"(fingerprint {rep.get('fingerprint', '')[:16]})")
        for s in rep.get("skipped") or []:
            print(f"  skipped {os.path.basename(s[0])} ({s[1]}): {s[2]}")
        post = rep.get("post") or {}
        if post.get("stale_count"):
            print(f"  warning: {post['stale_count']} stale source(s) remain after compile")
        return 0

    if args.sub == "rebuild":
        rep = K.rebuild_index(roots, atlas_dir)
        if not rep.get("ok"):
            print(f"rebuild failed: {rep.get('reason')} ({rep.get('error', '')})", file=sys.stderr)
            return 1
        print(f"full rebuild: {rep.get('sources', 0)} sources, "
              f"{rep.get('stats', {}).get('claims', 0)} claims "
              f"(fingerprint {rep.get('fingerprint', '')[:16]})")
        return 0

    if args.sub == "watch":
        def _on_compile(rep: dict) -> None:
            if not rep.get("ok"):
                print(f"  recompile failed: {rep.get('reason')} ({rep.get('error', '')})", file=sys.stderr)
                return
            print(f"  recompiled: {rep.get('sources', 0)} sources "
                  f"(fingerprint {rep.get('fingerprint', '')[:16]})")

        stop = K.watch_knowledge(roots, atlas_dir, interval=getattr(args, "interval", 2.0),
                                 on_compile=_on_compile)
        print(f"watching {roots} -> {atlas_dir} (interval {getattr(args, 'interval', 2.0)}s; Ctrl-C to stop)")
        try:
            while not stop.is_set():
                stop.wait(0.5)
        except KeyboardInterrupt:
            stop.set()
        print("watch stopped")
        return 0

    print("unknown knowledge subcommand", file=sys.stderr)
    return 2


def cmd_verify(args) -> int:
    store = _store()
    run = store.get("runs", args.run_id)
    if not run:
        print(f"no run {args.run_id!r}", file=sys.stderr)
        return 1
    worker = get_worker(run["worker"])
    eng = _engine(worker)
    from .procedures import load_procedure, procedure_verifications

    proc = None
    if run.get("procedure"):
        proc = load_procedure(worker, run["procedure"])
    specs = (proc and procedure_verifications(proc, {})) or run.get("verifications") or []
    if not specs:
        print("no verification checks declared for this run")
        return 0
    print(f"verifying run {args.run_id} ({len(specs)} checks):")
    any_fail = False
    results = []
    for spec in specs:
        v = eng.record_verification(run, spec, eng._tool_ctx(run))
        flag = "PASS" if v.outcome.value == "PASS" else ("FAIL" if v.outcome.value == "FAIL" else "UNVERIFIABLE")
        if flag == "FAIL":
            any_fail = True
        results.append({"check": v.check, "outcome": flag, "detail": v.detail})
        print(f"  [{flag}] {v.check}: {v.detail}")
    if _wants_json(args):
        _jprint({"run_id": args.run_id, "all_passed": not any_fail, "results": results})
    return 1 if any_fail else 0


def cmd_connectors(args) -> int:
    """§20 — inspect a worker's governed external access."""
    worker = get_worker(args.worker)
    eng = _engine(worker)
    if args.sub == "check":
        res = eng.connector_action(args.kind, args.action, args.target)
        if res.get("ok"):
            print(f"ALLOWED {args.kind}:{args.action} -> {args.target}")
            print(f"  credentials in play: {', '.join(res.get('credentials_used') or []) or '(none)'}")
        else:
            print(f"REFUSED {args.kind}:{args.action} -> {args.target}", file=sys.stderr)
            print(f"  reason: {res.get('reason')}", file=sys.stderr)
            return 1
        return 0
    # list
    desc = eng.connectors.describe()
    if _wants_json(args):
        _jprint({"worker": worker.name, "connectors": desc or {}})
        return 0
    if not desc:
        print(f"worker {worker.name!r} has NO enabled connectors (default-deny: all external access blocked)")
        return 0
    print(f"worker {worker.name!r} enabled connectors (default-deny; targets gated by allow-list):")
    for name, d in desc.items():
        allow = d.get("allow") or []
        creds = d.get("credentials_required") or []
        print(f"  - {name}: allow={allow} credentials={creds}")
    return 0


def cmd_browser(args) -> int:
    """§21 — inspect a worker's browser hardening policy (default-deny)."""
    worker = get_worker(args.worker)
    if _wants_json(args):
        _jprint({"worker": worker.name, "browser_allow": worker.browser_allow,
                 "browser_timeout": worker.browser_timeout,
                 "browser_downloads": worker.browser_downloads,
                 "browser_uploads": worker.browser_uploads,
                 "browser_credential_refs": worker.browser_credential_refs,
                 "browser_private_session": worker.browser_private_session})
        return 0
    print(f"worker {worker.name!r} browser hardening (default-deny; nothing permitted unless listed):")
    print(f"  url allow-list : {worker.browser_allow or '(empty: ALL urls denied)'}")
    print(f"  open timeout   : {worker.browser_timeout}s (hard ceiling on every browser.open)")
    print(f"  downloads      : {'ENABLED' if worker.browser_downloads else 'disabled'}")
    print(f"  uploads        : {'ENABLED' if worker.browser_uploads else 'disabled'}")
    print(f"  credentials    : {worker.browser_credential_refs or '(none — no injected auth)'}")
    print(f"  private session: {'yes (isolated, no shared cookies/profile)' if worker.browser_private_session else 'NO (shared profile) — set browser_private_session: true'}")
    return 0


def cmd_message(args) -> int:
    """§22 — inspect a worker's messaging policy (default-deny channel)."""
    worker = get_worker(args.worker)
    if _wants_json(args):
        _jprint({"worker": worker.name, "message_allow": worker.message_allow,
                 "message_rate_limit": worker.message_rate_limit,
                 "delivery": "approval_required"})
        return 0
    print(f"worker {worker.name!r} messaging policy (default-deny; nothing delivered unless listed):")
    print(f"  channel allow-list : {worker.message_allow or '(empty: ALL channels denied)'}")
    print(f"  rate limit         : {worker.message_rate_limit if worker.message_rate_limit else 'unlimited (bounded only by max_actions)'}")
    print(f"  delivery           : requires approval (message.send is EXTERNAL + requires_approval)")
    print(f"  draft mode         : supported (message.send draft=true composes without delivering)")
    return 0


def cmd_egress(args) -> int:
    """§54 — inspect a worker's network egress policy (default-deny host list)."""
    worker = get_worker(args.worker)
    if _wants_json(args):
        _jprint({"worker": worker.name, "egress_allow": worker.egress_allow,
                 "ssrf_guard": "metadata/link-local/private ranges always blocked"})
        return 0
    print(f"worker {worker.name!r} network egress policy (default-deny; nothing egresses unless listed):")
    print(f"  host allow-list : {worker.egress_allow or '(empty: ALL egress denied)'}")
    print(f"  ssrf guard      : metadata/link-local/private ranges always blocked")
    return 0


def cmd_dlp(args) -> int:
    """§55 — inspect a worker's DLP (data-loss-prevention) policy."""
    from .dlp import BUILTIN_DLP_RULES
    worker = get_worker(args.worker)
    if _wants_json(args):
        _jprint({"worker": worker.name, "dlp_rules": worker.dlp_rules or [],
                 "catalog": sorted(BUILTIN_DLP_RULES)})
        return 0
    print(f"worker {worker.name!r} DLP policy (opt-in; empty dlp_rules = no scanning):")
    print(f"  active rules    : {worker.dlp_rules or '(none — egress payloads are not scanned)'}")
    if worker.dlp_rules:
        for n in worker.dlp_rules:
            rule = BUILTIN_DLP_RULES.get(n)
            print(f"    - {n} ({rule.kind if rule else 'UNKNOWN'})")
    print(f"  catalog         : {sorted(BUILTIN_DLP_RULES)}")
    return 0


def cmd_procedure(args) -> int:
    """§23 — procedure publish / rollback / list (permissions via RBAC)."""
    from . import procedures as P
    from .rbac import RBAC
    worker = get_worker(args.worker)
    rbac = RBAC()
    if args.sub == "list":
        published = P.list_published(worker)
        if _wants_json(args):
            _jprint(published)
            return 0
        if not published:
            print(f"no published procedures for {worker.name!r}")
            return 0
        for p in published:
            cur = P.current_version(worker, p["name"])
            mark = " (current)" if p["version"] == cur else ""
            print(f"  {p['name']} v{p['version']}{mark} by {p['author'] or '(unknown)'} [{p['hash'][:12]}]")
        return 0
    if args.sub == "publish":
        if not P.can_publish(rbac, args.role):
            print(f"error: role {args.role!r} lacks 'procedure:publish' (RBAC denied)", file=sys.stderr)
            return 3
        body = args.body
        if args.file:
            with open(args.file, "r", encoding="utf-8") as fh:
                body = fh.read()
        if not body:
            print("error: --body or --file is required to publish", file=sys.stderr)
            return 2
        info = P.publish_procedure(worker, args.name, body, author=args.author)
        print(f"published {info['name']} v{info['version']} (hash {info['hash'][:12]})")
        return 0
    if args.sub == "rollback":
        if not P.can_publish(rbac, args.role):
            print(f"error: role {args.role!r} lacks 'procedure:publish' (RBAC denied)", file=sys.stderr)
            return 3
        info = P.rollback_procedure(worker, args.name, version=args.version or "")
        print(f"rolled back {info['name']} to v{info['version']}")
        return 0
    print(f"unknown procedure subcommand {args.sub!r}", file=sys.stderr)
    return 2


def cmd_worker_lifecycle(args) -> int:
    """§26 — worker lifecycle: enable / disable / clone / archive / export / import."""
    from . import lifecycle as L
    from .config import default_workspace, Workspace
    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        if args.sub == "enable":
            w = L.set_enabled(ws, args.worker, disabled=False)
            print(f"enabled worker {w.name!r}")
        elif args.sub == "disable":
            w = L.set_enabled(ws, args.worker, disabled=True)
            print(f"disabled worker {w.name!r} (runs are now refused)")
        elif args.sub == "clone":
            w = L.clone(ws, args.src, args.dst, force=args.force)
            print(f"cloned {args.src!r} -> {w.name!r}")
        elif args.sub == "archive":
            dest = L.archive(ws, args.worker)
            print(f"archived {args.worker!r} to {dest}")
        elif args.sub == "export":
            dest = L.export_worker(ws, args.worker, args.file)
            print(f"exported {args.worker!r} -> {dest}")
        elif args.sub == "import":
            w = L.import_worker(ws, args.file, force=args.force)
            print(f"imported worker {w.name!r}")
        elif args.sub == "versions":
            vs = L.list_versions(ws, args.worker)
            if not vs:
                print(f"no version history for {args.worker!r}")
            else:
                print(f"version history for {args.worker!r}:")
                for v in vs:
                    print(f"  {v}")
        else:
            print(f"unknown worker subcommand {args.sub!r}", file=sys.stderr)
            return 2
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_trigger(args) -> int:
    """§24 — validate / watch / serve workflow triggers for a worker."""
    from . import trigger as T
    from .config import default_workspace, get_worker, Workspace
    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        worker = get_worker(args.worker, ws)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        triggers = T.resolve_triggers(worker)
    except T.TriggerError as e:
        print(f"error: invalid trigger on {worker.name!r}: {e}", file=sys.stderr)
        return 2
    if args.sub == "validate":
        if not triggers:
            print(f"{worker.name!r} has no triggers")
        else:
            for t in triggers:
                print(f"  {t['kind']}: {t}")
        return 0
    if args.sub == "watch":
        fired: List[str] = []
        def on_fire(trig, paths):
            fired.append(trig["kind"])
            print(f"[trigger {trig['kind']}] changed: {paths}")
        stop = threading.Event()
        print(f"watching triggers for {worker.name!r} (Ctrl-C to stop)...")
        watchers = [
            T.FileWatcher(t, on_fire, stop) for t in triggers if t["kind"] == "file_changed"
        ]
        for w in watchers:
            w.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
            for w in watchers:
                w.stop()
        return 0
    if args.sub == "serve":
        recv = T.WebhookReceiver(host=args.host, port=args.port)
        webhooks = [t for t in triggers if t["kind"] == "webhook"]
        if not webhooks:
            print(f"{worker.name!r} has no webhook triggers", file=sys.stderr)
            return 1
        def fired_cb(trig, payload):
            print(f"[webhook {trig['path']}] {payload}")
        recv.on_fire = fired_cb
        for t in webhooks:
            recv.register(t)
        print(f"serving webhooks for {worker.name!r} on {args.host}:{args.port}")
        try:
            recv.serve_forever()
        except KeyboardInterrupt:
            recv.shutdown()
        return 0
    print(f"unknown trigger subcommand {args.sub!r}", file=sys.stderr)
    return 2


def cmd_template(args) -> int:
    """§25 — worker templates / marketplace scaffolding."""
    from . import templates as T
    from .config import default_workspace, Workspace
    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        if args.sub == "list":
            for name in T.list_templates():
                print(name)
        elif args.sub == "create":
            w = T.create_worker(ws, args.template, args.name, args.goal or "", force=args.force)
            print(f"created worker {w.name!r} from template {args.template!r}")
        elif args.sub == "market":
            if args.msub == "list":
                items = T.list_marketplace(ws)
                if not items:
                    print("(marketplace empty)")
                else:
                    for n in items:
                        print(n)
            elif args.msub == "publish":
                dest = T.publish_to_marketplace(ws, args.name)
                print(f"published to marketplace: {dest}")
            elif args.msub == "import":
                w = T.import_from_marketplace(ws, args.name, force=args.force)
                print(f"imported {w.name!r} from marketplace")
            else:
                print(f"unknown market subcommand {args.msub!r}", file=sys.stderr)
                return 2
        else:
            print(f"unknown template subcommand {args.sub!r}", file=sys.stderr)
            return 2
    except (FileExistsError, FileNotFoundError, T.TemplateError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_explain(args) -> int:
    """§28/§29 — explain a request (plan + permissions, no execution)."""
    from . import explain as E
    from .config import default_workspace, get_worker, Workspace
    from .engine import WorkerEngine, WorkerStore

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        worker = get_worker(args.worker, ws)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    eng = WorkerEngine(worker, WorkerStore(ws.state_dir))
    try:
        res = E.explain(eng, args.request, procedure=args.procedure or "")
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))
        return 0
    print(f"intent: {res.intent}")
    print(f"planner: {res.planner}")
    print(f"would require approval: {res.would_require_approval}")
    print(f"would be blocked: {res.would_be_blocked}")
    for s in res.steps:
        print(f"  [{s.index}] {s.tool or '(reason)'} -> {s.decision} ({s.risk})")
        if s.reason:
            print(f"        {s.reason}")
    return 0


def cmd_replay(args) -> int:
    """§30 — replay a run in explain (ledger) or rerun (execute) mode."""
    from . import explain as E
    from .config import default_workspace, Workspace
    from .engine import WorkerEngine, WorkerStore
    from .config import get_worker

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    # find the worker that owns the run by scanning; engine needs a worker.
    worker = get_worker(args.worker, ws) if getattr(args, "worker", None) else None
    if worker is None:
        workers = ws_list(getattr(args, "home", None))
        if not workers:
            print("error: no workers in workspace", file=sys.stderr)
            return 1
        worker = workers[0]
    eng = WorkerEngine(worker, WorkerStore(ws.state_dir))
    try:
        rep = E.replay(eng, args.run_id, mode=args.mode)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rep, indent=2))
    return 0


def ws_list(home=None):
    from .config import default_workspace, list_workers, Workspace

    ws = Workspace(home) if home else default_workspace()
    return list_workers(ws)


def cmd_doctor(args) -> int:
    """§33 — workspace health check."""
    from . import doctor as D
    from .config import default_workspace, Workspace

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    rep = D.run_doctor(ws)
    if getattr(args, "json", False):
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 1
    for c in rep["checks"]:
        print(f"[{c['status'].upper():5}] {c['label']}: {c['detail']}")
    if rep["ok"]:
        print(f"\ndoctor: OK ({rep['warnings']} warning(s))")
        return 0
    print(f"\ndoctor: FAIL ({rep['errors']} error(s), {rep['warnings']} warning(s))")
    return 1


def cmd_package(args) -> int:
    """§31 — export/import a portable workspace package."""
    from . import package as P
    from .config import default_workspace, Workspace

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        if args.sub == "export":
            dest = P.export_package(ws, args.file)
            print(f"exported package -> {dest}")
        elif args.sub == "import":
            info = P.import_package(args.file, ws, force=args.force)
            print(f"imported {info['extracted']} entries into {info['root']}")
        else:
            print(f"unknown package subcommand {args.sub!r}", file=sys.stderr)
            return 2
    except (FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_backup(args) -> int:
    """§32 — backup / restore (state + workers, no secrets key)."""
    from . import package as P
    from .config import default_workspace, Workspace

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    try:
        if args.sub == "backup":
            dest = P.backup(ws, args.file)
            print(f"backup -> {dest}")
        elif args.sub == "restore":
            info = P.restore(args.file, ws, force=args.force)
            print(f"restored {info['extracted']} entries into {info['root']}")
        else:
            print(f"unknown backup subcommand {args.sub!r}", file=sys.stderr)
            return 2
    except (FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_metrics(args) -> int:
    """§34 — show observability counters."""
    from . import metrics as M

    print(json.dumps(M.snapshot(), indent=2))
    return 0


def cmd_web(args) -> int:
    web_mod.serve(port=args.port, home=args.home, host=getattr(args, "host", "127.0.0.1"))
    return 0


# --- Auth / RBAC / policy / secrets hooks (Phase 1) -------------------------

def _auth() -> AuthProvider:
    return AuthProvider(_store())


def cmd_user(args) -> int:
    ap = _auth()
    if args.user_sub == "add":
        if not args.password:
            print("error: --password is required for user add", file=sys.stderr)
            return 2
        role = args.role or "analyst"
        try:
            ap.create_user(args.username, args.password, role=role)
            print(f"created user {args.username} (role={role})")
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if args.user_sub == "disable":
        try:
            ap.disable_user(args.username)
            print(f"disabled user {args.username}")
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if args.user_sub == "enable":
        try:
            ap.enable_user(args.username)
            print(f"enabled user {args.username}")
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    # list
    users = ap.list_users()
    if _wants_json(args):
        _jprint([{"username": u.username, "role": u.role, "disabled": u.disabled}
                 for u in users])
        return 0
    for u in users:
        print(f"  {u.username:<20} role={u.role:<10} disabled={u.disabled}")
    return 0


def cmd_policy(args) -> int:
    ps = PolicyStore(_store())
    if args.policy_sub == "publish":
        body = {
            "read": args.read or "auto",
            "reversible": args.reversible or "auto",
            "external": args.external or "approve",
            "financial": args.financial or "approve",
            "destructive": args.destructive or "deny",
        }
        p = ps.publish(body, args.scope)
        print(f"published policy {p.hash} v{p.version} scope={args.scope}")
        return 0
    if args.policy_sub == "list":
        rows = [{"version": p.version, "hash": p.hash, "body": p.body}
                for p in ps.list_versions(args.scope)]
        if _wants_json(args):
            _jprint(rows)
            return 0
        for p in rows:
            print(f"  v{p.version} {p.hash} {p.body}")
        return 0
    cur = ps.latest(args.scope)
    out = {"scope": args.scope, "hash": cur.hash if cur else None,
           "version": cur.version if cur else None, "body": cur.body if cur else None}
    if _wants_json(args):
        _jprint(out)
        return 0
    print(f"current policy for {args.scope}: {cur.hash if cur else '(none)'}")
    return 0


def cmd_secret(args) -> int:
    try:
        ss = SecretStore(_store())
    except EncryptionUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    if args.secret_sub == "set":
        ss.set(args.name, args.value)
        print(f"stored secret {args.name} (fingerprint {_fp_of(ss, args.name)})")
        return 0
    if args.secret_sub == "delete":
        ss.delete(args.name)
        print(f"deleted secret {args.name}")
        return 0
    if args.secret_sub == "redact":
        text = args.text or sys.stdin.read()
        print(ss.redact(text) if ss.list_names() else redact_static(text), end="")
        return 0
    for n in ss.list_names():
        print(f"  {n:<20} {ss.get(n)[:1] + '***' if ss.exists(n) else ''}")
    return 0


def _fp_of(ss: SecretStore, name: str) -> str:
    rec = ss.store.get("secrets", name)
    return rec["fingerprint"] if rec else "?"


def cmd_safemode(args) -> int:
    """§62 — view / change the workspace safe-mode level."""
    from .safemode import SafeMode, READONLY

    sm = SafeMode(_store())
    sub = getattr(args, "safemode_sub", "status")
    if sub == "on":
        lv = sm.enable()
    elif sub == "off":
        lv = sm.disable()
    elif sub == "readonly":
        lv = sm.set_level(READONLY)
    elif sub == "locked":
        lv = sm.lock()
    else:  # status
        st = sm.status_dict()
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2))
        else:
            print(f"safe mode: {'ENABLED' if st['enabled'] else 'off'} (level={st['level']})")
            print(f"  policy: {st['policy']}")
        return 0
    print(f"safe mode -> {lv}")
    return 0


def cmd_security(args) -> int:
    """§64 — show the curated security-event feed for the workspace."""
    from .security_events import SecurityEvents
    from .store import WorkerStore
    from .config import default_workspace, Workspace

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    store = WorkerStore(ws.state_dir)
    sec = SecurityEvents(store)
    kinds = getattr(args, "kind", None)
    kinds = [kinds] if kinds else None
    events = sec.recent(limit=getattr(args, "limit", 50), kinds=kinds)
    if getattr(args, "json", False):
        print(json.dumps(_store_security_payload(store, sec), indent=2))
        return 0
    chain = store.verify_audit_chain()
    print(f"audit chain: {'OK' if chain.get('ok') else 'BROKEN'} "
          f"({chain.get('checked', 0)}/{chain.get('lines', 0)} lines)")
    print(f"counts by kind: {sec.counts_by_kind()}")
    print(f"--- {len(events)} security event(s) ---")
    for e in events:
        print(f"  [{e['severity']:<8}] {e['kind']:<10} {e['label']} "
              f"| {e['summary']}  (by {e['actor']})")
    return 0


def _store_security_payload(store, sec):  # pragma: no cover - convenience
    return {"audit_chain_ok": store.verify_audit_chain().get("ok"),
            "events": sec.recent(limit=100)}


def cmd_incident(args) -> int:
    """§63 — declare / close an incident, or freeze the platform (lockdown)."""
    from .incident import IncidentLedger

    led = IncidentLedger(_store())
    sub = getattr(args, "incident_sub", "status")
    if sub == "open":
        try:
            res = led.open(getattr(args, "summary", "incident opened via CLI"),
                           by=getattr(args, "by", "operator"))
        except ValueError as e:
            print(f"refused: {e}")
            return 1
        print(f"incident OPEN (id={res['incident_id']}); safe mode -> {res['safe_mode']}")
        return 0
    if sub == "lockdown":
        res = led.lockdown(getattr(args, "summary", "platform lockdown via CLI"),
                           by=getattr(args, "by", "operator"))
        print(f"incident LOCKDOWN; safe mode -> {res['safe_mode']}")
        return 0
    if sub == "close":
        res = led.close(by=getattr(args, "by", "operator"), note=getattr(args, "note", ""))
        if not res["changed"]:
            print("no active incident to close")
            return 0
        print(f"incident CLOSED; safe mode still -> {res['safe_mode']} "
              f"(stand down explicitly: sworker safemode off)")
        return 0
    # status (and --timeline)
    st = led.status_dict()
    if getattr(args, "json", False):
        if getattr(args, "timeline", False):
            st = {"status": st, "timeline": led.list_incidents()}
        print(json.dumps(st, indent=2))
    else:
        print(f"incident: {'ACTIVE' if st['active'] else 'inactive'} "
              f"(safe mode={st['safe_mode']})")
        print(f"  policy: {st['policy']}")
        if getattr(args, "timeline", False):
            for ev in led.list_incidents():
                print(f"  - {ev['event']} by {ev['by']} @ {ev['ts']}: {ev.get('summary') or ev.get('note')}")
    return 0


def cmd_migrate(args) -> int:
    """§60 — upgrade a stored workspace's data to the current version."""
    from . import migrations as M
    from .config import default_workspace, Workspace
    from .store import WorkerStore

    ws = Workspace(args.home) if getattr(args, "home", None) else default_workspace()
    store = WorkerStore(ws.state_dir)
    cur = M.current_version(store)
    if getattr(args, "dry_run", False):
        pend = M.pending(store)
        out = {"current": cur, "target": M.DATA_VERSION,
               "pending": pend, "would_apply": [M.MIGRATIONS[v][0] for v in pend]}
        if getattr(args, "json", False):
            print(json.dumps(out, indent=2))
        else:
            print(f"data version: {cur} (current platform: {M.DATA_VERSION})")
            if pend:
                print("pending migrations:")
                for v in pend:
                    print(f"  -> v{v}: {M.MIGRATIONS[v][0]}")
            else:
                print("nothing to migrate")
        return 0
    try:
        applied = M.migrate(store, to_version=args.to)
    except M.MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = {"current": M.current_version(store), "applied": applied}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        if applied:
            print(f"migrated to data version {M.current_version(store)}")
            for v in applied:
                print(f"  applied v{v}: {M.MIGRATIONS[v][0]}")
        else:
            print(f"already at data version {M.current_version(store)}; nothing to do")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sworker", description="Sovereign AI Worker Platform (local-first)")
    p.add_argument("--version", action="version", version=f"sworker {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="scaffold a workspace").set_defaults(func=cmd_init)
    ob = sub.add_parser("onboard", help="guided first-run setup (§46)")
    ob.add_argument("--username", default="")
    ob.add_argument("--password", default="")
    ob.set_defaults(func=cmd_onboard)

    workers_p = sub.add_parser("workers", help="list workers")
    workers_p.add_argument("--json", action="store_true")
    workers_p.set_defaults(func=cmd_workers)
    s = sub.add_parser("show", help="show worker identity")
    s.add_argument("worker"); s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_show)

    r = sub.add_parser("run", help="run a request")
    r.add_argument("worker"); r.add_argument("request")
    r.add_argument("-i", "--input", action="append", help="key=value run inputs")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("approve", help="approve a pending approval")
    a.add_argument("appr_id"); a.add_argument("--note", default="")
    a.add_argument("--by", default="cli", help="approver identity (for quorum, distinct humans)")
    a.add_argument("--role", default="", help="approver RBAC role (must satisfy min_role)")
    a.set_defaults(func=cmd_approve)
    d = sub.add_parser("deny", help="deny a pending approval")
    d.add_argument("appr_id"); d.add_argument("--note", default="")
    d.add_argument("--by", default="cli", help="approver identity (for quorum, distinct humans)")
    d.add_argument("--role", default="", help="approver RBAC role (must satisfy min_role)")
    d.set_defaults(func=cmd_deny)

    rs = sub.add_parser("resume", help="resume a run after approval")
    rs.add_argument("run_id"); rs.set_defaults(func=cmd_resume)

    rn = sub.add_parser("runs", help="list runs")
    rn.add_argument("worker", nargs="?"); rn.add_argument("-n", "--limit", type=int, default=20); rn.add_argument("--json", action="store_true"); rn.set_defaults(func=cmd_runs)
    rsh = sub.add_parser("run-info", help="show one run"); rsh.add_argument("run_id"); rsh.add_argument("--json", action="store_true"); rsh.set_defaults(func=cmd_run_show)
    wy = sub.add_parser("why", help="§65 explain why a run / workspace is blocked"); wy.add_argument("run_id", nargs="?"); wy.add_argument("--workspace", action="store_true"); wy.add_argument("--json", action="store_true"); wy.add_argument("--home", default=None); wy.set_defaults(func=cmd_why)

    au = sub.add_parser("audit", help="replay event log for a run")
    au.add_argument("run_id"); au.add_argument("--json", action="store_true"); au.set_defaults(func=cmd_audit)

    l = sub.add_parser("learn", help="capture a run as a procedure")
    l.add_argument("run_id"); l.add_argument("name"); l.set_defaults(func=cmd_learn)

    pr = sub.add_parser("proc", help="list procedures"); pr.add_argument("worker", nargs="?"); pr.add_argument("--json", action="store_true"); pr.set_defaults(func=cmd_proc)

    sc = sub.add_parser("sched", help="manage schedules")
    scsub = sc.add_subparsers(dest="sub", required=True)
    scadd = scsub.add_parser("add"); scadd.add_argument("worker"); scadd.add_argument("procedure"); scadd.add_argument("cron"); scadd.set_defaults(func=cmd_sched)
    scoff = scsub.add_parser("off"); scoff.add_argument("id"); scoff.set_defaults(func=cmd_sched)
    sc_list = scsub.add_parser("list"); sc_list.add_argument("--json", action="store_true")
    sc_list.set_defaults(func=cmd_sched, sub="list")
    sc.set_defaults(sub="list")

    v = sub.add_parser("verify", help="run verification checks for a run")
    v.add_argument("run_id"); v.add_argument("--json", action="store_true"); v.set_defaults(func=cmd_verify)

    # --- §20 connector architecture: inspect governed external access ----------
    cn = sub.add_parser("connectors", help="inspect §20 connector policy for a worker")
    cnsub = cn.add_subparsers(dest="sub", required=True)
    cnl = cnsub.add_parser("list"); cnl.add_argument("worker"); cnl.add_argument("--json", action="store_true"); cnl.set_defaults(func=cmd_connectors)
    cnc = cnsub.add_parser("check")
    cnc.add_argument("worker"); cnc.add_argument("kind"); cnc.add_argument("target")
    cnc.add_argument("--action", default="send"); cnc.set_defaults(func=cmd_connectors, sub="check")
    cn.set_defaults(sub="list")

    # --- §21 browser hardening: inspect governed browser policy -----------------
    br = sub.add_parser("browser", help="inspect §21 browser hardening policy for a worker")
    brs = br.add_subparsers(dest="sub", required=True)
    brp = brs.add_parser("policy"); brp.add_argument("worker"); brp.add_argument("--json", action="store_true"); brp.set_defaults(func=cmd_browser)
    br.set_defaults(sub="policy")

    # --- §22 messaging policy: inspect governed message policy -----------------
    ms = sub.add_parser("message", help="inspect §22 messaging policy for a worker")
    mss = ms.add_subparsers(dest="sub", required=True)
    msp = mss.add_parser("policy"); msp.add_argument("worker"); msp.add_argument("--json", action="store_true"); msp.set_defaults(func=cmd_message)
    ms.set_defaults(sub="policy")

    # --- §54 network egress registry: inspect governed egress policy ------------
    eg = sub.add_parser("egress", help="inspect §54 network egress policy for a worker")
    egs = eg.add_subparsers(dest="sub", required=True)
    egp = egs.add_parser("policy"); egp.add_argument("worker"); egp.add_argument("--json", action="store_true"); egp.set_defaults(func=cmd_egress)
    eg.set_defaults(sub="policy")

    # --- §55 DLP primitives: inspect governed DLP policy -----------------------
    dp = sub.add_parser("dlp", help="inspect §55 DLP (data-loss-prevention) policy for a worker")
    dps = dp.add_subparsers(dest="sub", required=True)
    dpp = dps.add_parser("policy"); dpp.add_argument("worker"); dpp.add_argument("--json", action="store_true"); dpp.set_defaults(func=cmd_dlp)
    dp.set_defaults(sub="policy")

    # --- §23 procedure publish/rollback/list (RBAC-gated) ----------------------
    pr = sub.add_parser("procedure", help="§23 procedure publish/rollback/list (RBAC-gated)")
    prs = pr.add_subparsers(dest="sub", required=True)
    prl = prs.add_parser("list"); prl.add_argument("worker"); prl.add_argument("--json", action="store_true"); prl.set_defaults(func=cmd_procedure)
    prp = prs.add_parser("publish")
    prp.add_argument("worker"); prp.add_argument("name")
    prp.add_argument("--body", default="", help="YAML body inline")
    prp.add_argument("--file", default="", help="YAML file to publish")
    prp.add_argument("--author", default="")
    prp.add_argument("--role", default="operator", help="RBAC role for the publish gate")
    prp.set_defaults(func=cmd_procedure)
    prr = prs.add_parser("rollback")
    prr.add_argument("worker"); prr.add_argument("name")
    prr.add_argument("--version", default="", help="target version; omit to roll back one")
    prr.add_argument("--role", default="operator", help="RBAC role for the publish gate")
    prr.set_defaults(func=cmd_procedure)

    # --- §26 worker lifecycle: enable/disable/clone/archive/export/import -----
    wk = sub.add_parser("worker", help="§26 worker lifecycle (enable/disable/clone/archive/export/import)")
    wks = wk.add_subparsers(dest="sub", required=True)
    wke = wks.add_parser("enable"); wke.add_argument("worker"); wke.add_argument("--home", default=None)
    wke.set_defaults(func=cmd_worker_lifecycle)
    wkd = wks.add_parser("disable"); wkd.add_argument("worker"); wkd.add_argument("--home", default=None)
    wkd.set_defaults(func=cmd_worker_lifecycle)
    wkc = wks.add_parser("clone"); wkc.add_argument("src"); wkc.add_argument("dst")
    wkc.add_argument("--force", action="store_true"); wkc.add_argument("--home", default=None)
    wkc.set_defaults(func=cmd_worker_lifecycle)
    wka = wks.add_parser("archive"); wka.add_argument("worker"); wka.add_argument("--home", default=None)
    wka.set_defaults(func=cmd_worker_lifecycle)
    wkx = wks.add_parser("export"); wkx.add_argument("worker"); wkx.add_argument("file")
    wkx.add_argument("--home", default=None); wkx.set_defaults(func=cmd_worker_lifecycle)
    wki = wks.add_parser("import"); wki.add_argument("file")
    wki.add_argument("--force", action="store_true"); wki.add_argument("--home", default=None)
    wki.set_defaults(func=cmd_worker_lifecycle)
    wkv = wks.add_parser("versions"); wkv.add_argument("worker"); wkv.add_argument("--home", default=None)
    wkv.set_defaults(func=cmd_worker_lifecycle)

    # --- §24 workflow triggers: validate / watch / serve -----------------------
    tr = sub.add_parser("trigger", help="§24 workflow triggers (file_changed/webhook/event)")
    trs = tr.add_subparsers(dest="sub", required=True)
    trv = trs.add_parser("validate"); trv.add_argument("worker"); trv.add_argument("--home", default=None)
    trv.set_defaults(func=cmd_trigger)
    trw = trs.add_parser("watch"); trw.add_argument("worker"); trw.add_argument("--home", default=None)
    trw.set_defaults(func=cmd_trigger)
    trsrv = trs.add_parser("serve"); trsrv.add_argument("worker")
    trsrv.add_argument("--host", default="127.0.0.1"); trsrv.add_argument("--port", type=int, default=8787)
    trsrv.add_argument("--home", default=None); trsrv.set_defaults(func=cmd_trigger)

    # --- §25 worker templates / marketplace -----------------------------------
    tp = sub.add_parser("template", help="§25 worker templates / marketplace")
    tps = tp.add_subparsers(dest="sub", required=True)
    tpl = tps.add_parser("list"); tpl.add_argument("--home", default=None)
    tpl.set_defaults(func=cmd_template)
    tpc = tps.add_parser("create")
    tpc.add_argument("template"); tpc.add_argument("name"); tpc.add_argument("--goal", default="")
    tpc.add_argument("--force", action="store_true"); tpc.add_argument("--home", default=None)
    tpc.set_defaults(func=cmd_template)
    tpm = tps.add_parser("market"); tpms = tpm.add_subparsers(dest="msub", required=True)
    tpml = tpms.add_parser("list"); tpml.add_argument("--home", default=None); tpml.set_defaults(func=cmd_template)
    tpmp = tpms.add_parser("publish"); tpmp.add_argument("name"); tpmp.add_argument("--home", default=None)
    tpmp.set_defaults(func=cmd_template)
    tpmi = tpms.add_parser("import"); tpmi.add_argument("name")
    tpmi.add_argument("--force", action="store_true"); tpmi.add_argument("--home", default=None)
    tpmi.set_defaults(func=cmd_template)

    # --- §28/§29 explain (dry-run) -------------------------------------------
    ex = sub.add_parser("explain", help="§28/§29 explain a request (plan + perms, no run)")
    ex.add_argument("worker"); ex.add_argument("request"); ex.add_argument("--procedure", default="")
    ex.add_argument("--json", action="store_true"); ex.add_argument("--home", default=None)
    ex.set_defaults(func=cmd_explain)

    # --- §30 replay (explain ledger / rerun) ---------------------------------
    rp = sub.add_parser("replay", help="§30 replay a run (explain|rerun)")
    rp.add_argument("run_id"); rp.add_argument("--mode", default="explain", choices=["explain", "rerun"])
    rp.add_argument("--worker", default=""); rp.add_argument("--home", default=None)
    rp.set_defaults(func=cmd_replay)

    # --- §33 doctor ----------------------------------------------------------
    dc = sub.add_parser("doctor", help="§33 workspace health check")
    dc.add_argument("--json", action="store_true"); dc.add_argument("--home", default=None)
    dc.set_defaults(func=cmd_doctor)

    # --- §60 data migrations -------------------------------------------------
    mg = sub.add_parser("migrate", help="§60 upgrade stored workspace data to current version")
    mg.add_argument("--to", type=int, default=None, help="target data version (default: current)")
    mg.add_argument("--dry-run", action="store_true", help="list pending steps without applying")
    mg.add_argument("--json", action="store_true"); mg.add_argument("--home", default=None)
    mg.set_defaults(func=cmd_migrate)

    # --- §31 export/import package -------------------------------------------
    pk = sub.add_parser("package", help="§31 export/import workspace package")
    pks = pk.add_subparsers(dest="sub", required=True)
    pke = pks.add_parser("export"); pke.add_argument("file"); pke.add_argument("--home", default=None)
    pke.set_defaults(func=cmd_package)
    pki = pks.add_parser("import"); pki.add_argument("file"); pki.add_argument("--force", action="store_true")
    pki.add_argument("--home", default=None); pki.set_defaults(func=cmd_package)

    # --- §32 backup/restore --------------------------------------------------
    bk = sub.add_parser("backup", help="§32 backup/restore workspace")
    bks = bk.add_subparsers(dest="sub", required=True)
    bke = bks.add_parser("backup"); bke.add_argument("file"); bke.add_argument("--home", default=None)
    bke.set_defaults(func=cmd_backup)
    bkr = bks.add_parser("restore"); bkr.add_argument("file"); bkr.add_argument("--force", action="store_true")
    bkr.add_argument("--home", default=None); bkr.set_defaults(func=cmd_backup)

    # --- §34 metrics ---------------------------------------------------------
    mt = sub.add_parser("metrics", help="§34 observability counters")
    mt.set_defaults(func=cmd_metrics)

    # --- §17 Atlas deepening: knowledge index management -----------------------
    kn = sub.add_parser("knowledge", help="company knowledge index (§17)")
    knsub = kn.add_subparsers(dest="sub", required=True)
    kns = knsub.add_parser("status", help="show index state + stale/missing sources")
    kns.add_argument("--home", default=None); kns.add_argument("--root", action="append", default=[]); kns.add_argument("--json", action="store_true")
    kns.set_defaults(func=cmd_knowledge)
    knc = knsub.add_parser("compile", help="incremental recompile (only changed sources)")
    knc.add_argument("--home", default=None); knc.add_argument("--root", action="append", default=[])
    knc.set_defaults(func=cmd_knowledge)
    knr = knsub.add_parser("rebuild", help="full wipe + recompile from scratch")
    knr.add_argument("--home", default=None); knr.add_argument("--root", action="append", default=[])
    knr.set_defaults(func=cmd_knowledge)
    knw = knsub.add_parser("watch", help="§19: watch sources and recompile on change")
    knw.add_argument("--home", default=None); knw.add_argument("--root", action="append", default=[])
    knw.add_argument("--interval", type=float, default=2.0, help="poll interval (seconds)")
    knw.set_defaults(func=cmd_knowledge)

    w = sub.add_parser("web", help="launch the local web UI")
    w.add_argument("--port", type=int, default=8799)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--home", default=None)
    w.set_defaults(func=cmd_web)

    # --- Phase 1 security subsystems ----------------------------------------
    usr = sub.add_parser("user", help="manage local users (auth/RBAC)")
    usub = usr.add_subparsers(dest="user_sub", required=False)
    ua = usub.add_parser("add"); ua.add_argument("username"); ua.add_argument("--password", default="")
    ua.add_argument("--role", default="analyst"); ua.set_defaults(func=cmd_user)
    ud = usub.add_parser("disable"); ud.add_argument("username"); ud.set_defaults(func=cmd_user)
    ue = usub.add_parser("enable"); ue.add_argument("username"); ue.set_defaults(func=cmd_user)
    ulist = usub.add_parser("list"); ulist.add_argument("--json", action="store_true"); ulist.set_defaults(func=cmd_user, user_sub="list")

    pol = sub.add_parser("policy", help="versioned immutable policies (spec §6)")
    pol.add_argument("--json", action="store_true")
    psub = pol.add_subparsers(dest="policy_sub", required=False)
    pp = psub.add_parser("publish"); pp.add_argument("scope")
    pp.add_argument("--read", default=None); pp.add_argument("--reversible", default=None)
    pp.add_argument("--external", default=None); pp.add_argument("--financial", default=None)
    pp.add_argument("--destructive", default=None); pp.add_argument("--json", action="store_true"); pp.set_defaults(func=cmd_policy)
    pl = psub.add_parser("list"); pl.add_argument("scope"); pl.add_argument("--json", action="store_true"); pl.set_defaults(func=cmd_policy)
    pc = psub.add_parser("current"); pc.add_argument("scope"); pc.add_argument("--json", action="store_true"); pc.set_defaults(func=cmd_policy)
    pol.set_defaults(policy_sub="current")

    sec = sub.add_parser("secret", help="encrypted secret store (spec §8)")
    ssub = sec.add_subparsers(dest="secret_sub", required=False)
    ss = ssub.add_parser("set"); ss.add_argument("name"); ss.add_argument("value"); ss.set_defaults(func=cmd_secret)
    sd = ssub.add_parser("delete"); sd.add_argument("name"); sd.set_defaults(func=cmd_secret)
    sr = ssub.add_parser("redact"); sr.add_argument("text", nargs="?", default=None); sr.set_defaults(func=cmd_secret)
    ssub.add_parser("list").set_defaults(func=cmd_secret, secret_sub="list")

    # --- §62 safe mode --------------------------------------------------------
    sm_ = sub.add_parser("safemode", help="§62 safe mode (freeze worker actions)")
    smsub = sm_.add_subparsers(dest="safemode_sub", required=False)
    sms = smsub.add_parser("status"); sms.add_argument("--json", action="store_true")
    sms.set_defaults(func=cmd_safemode)
    smon = smsub.add_parser("on"); smon.set_defaults(func=cmd_safemode, safemode_sub="on")
    smoff = smsub.add_parser("off"); smoff.set_defaults(func=cmd_safemode, safemode_sub="off")
    smro = smsub.add_parser("readonly"); smro.set_defaults(func=cmd_safemode, safemode_sub="readonly")
    smlk = smsub.add_parser("locked"); smlk.set_defaults(func=cmd_safemode, safemode_sub="locked")
    sm_.set_defaults(safemode_sub="status", func=cmd_safemode)

    # --- §63 incident response -----------------------------------------------
    inc = sub.add_parser("incident", help="§63 incident response (declare/close)")
    incsub = inc.add_subparsers(dest="incident_sub", required=False)
    incs = incsub.add_parser("status")
    incs.add_argument("--json", action="store_true")
    incs.add_argument("--timeline", action="store_true")
    incs.set_defaults(func=cmd_incident)
    incopen = incsub.add_parser("open")
    incopen.add_argument("summary", nargs="?", default="incident opened via CLI")
    incopen.add_argument("--by", default="operator")
    incopen.set_defaults(func=cmd_incident, incident_sub="open")
    inclk = incsub.add_parser("lockdown")
    inclk.add_argument("summary", nargs="?", default="platform lockdown via CLI")
    inclk.add_argument("--by", default="operator")
    inclk.set_defaults(func=cmd_incident, incident_sub="lockdown")
    incclose = incsub.add_parser("close")
    incclose.add_argument("--note", default="")
    incclose.add_argument("--by", default="operator")
    incclose.set_defaults(func=cmd_incident, incident_sub="close")
    inc.set_defaults(incident_sub="status", func=cmd_incident)

    # --- §64 security events ------------------------------------------------
    sec_ = sub.add_parser("security", help="§64 security-event feed")
    sec_.add_argument("--json", action="store_true")
    sec_.add_argument("--kind", default=None, help="filter to one event kind")
    sec_.add_argument("--limit", type=int, default=50)
    sec_.set_defaults(func=cmd_security)

    # --- §66 system status ------------------------------------------------
    st_ = sub.add_parser("status", help="§66 compose every hardening control into one verdict")
    st_.add_argument("--json", action="store_true")
    st_.set_defaults(func=cmd_status)

    # --- §58/§59 benchmarks ----------------------------------------------
    bm_ = sub.add_parser("benchmark", help="§58/§59 run deterministic perf/regression benchmarks")
    bm_.add_argument("--worker", default="acme-analyst")
    bm_.add_argument("--iterations", type=int, default=3)
    bm_.add_argument("--no-fail", action="store_true", help="report only; do not assert thresholds")
    bm_.add_argument("--json", action="store_true")
    bm_.set_defaults(func=cmd_benchmark)

    # --- §70 maturity model -----------------------------------------------
    mt_ = sub.add_parser("maturity", help="§70 score the deployment's hardening posture from real state")
    mt_.add_argument("--json", action="store_true")
    mt_.set_defaults(func=cmd_maturity)

    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
