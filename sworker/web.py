"""Minimal local-first web UI with real authentication + RBAC.

A single-file HTTP server built on stdlib ``http.server`` so the core keeps ZERO
third-party dependencies. It serves one page that lists workers, runs, and lets
you:

  * log in with a local account (``AuthProvider`` + session cookie);
  * submit a request to a worker (POST) and watch the run appear;
  * replay a run's audit trail + evidence + artifacts;
  * approve / reject a pending approval and then resume the run;
  * run a run's declared verification checks.

Everything reads from and writes to the same local store the CLI uses — there is
no separate database, nothing leaves the machine. The server binds to
``127.0.0.1`` only.

Security model (see docs/SECURITY.md for the full picture):
  * **Authentication**: every request needs a valid session cookie issued by
    ``/login``. Unknown/missing/expired/revoked cookies get a 401 to ``/login``.
  * **CSRF**: every state-changing request also requires a same-origin
    ``Origin``/``Referer`` header (loopback only). The startup token is gone —
    auth is by who you are, not a shared secret pasted into URLs.
  * **RBAC (server-side, fail-closed)**: each mutating route checks the logged-in
    user's role against the required capability. A viewer can read but cannot
    create runs or decide approvals; an operator can; an admin can do all.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets as _secrets
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlparse, parse_qs

from .config import default_workspace, get_worker, list_workers
from .store import WorkerStore
from .engine import WorkerEngine
from .approvals import ApprovalManager, ApprovalError
from .tools.http import render_egress_log
from .dlp import render_dlp_log
from .inference import NullInference
from .procedures import load_procedure, procedure_verifications
from .auth import AuthProvider
from .rbac import RBAC, PERMISSIONS
from . import metrics as _metrics
from . import doctor as _doctor
from . import explain as _explain


def _esc(s: str) -> str:
    return html.escape(str(s))


def _time(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title>"
        f"<style>"
        f"body{{font:14px/1.5 system-ui,Segoe UI,Arial;margin:0;background:#0f1115;color:#e6e6e6}}"
        f"a{{color:#7cc4ff;text-decoration:none}}a:hover{{text-decoration:underline}}"
        f"header{{padding:14px 20px;background:#161a21;border-bottom:1px solid #23272f}}"
        f"h1{{margin:0;font-size:18px}}h2{{font-size:15px;color:#aab}}"
        f".wrap{{padding:20px;max-width:1000px;margin:0 auto}}"
        f"table{{width:100%;border-collapse:collapse;margin:8px 0}}"
        f"th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid #23272f;vertical-align:top}}"
        f"th{{color:#8a93a3;font-weight:600;font-size:12px;text-transform:uppercase}}"
        f".pill{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#23272f}}"
        f".ok{{background:#173a23;color:#7ee2a8}}.bad{{background:#3a1717;color:#ff9a9a}}"
        f".warn{{background:#3a3417;color:#ffe08a}}.mono{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}"
        f".pend{{background:#2a2438;color:#c9a8ff}}code{{background:#1b1f27;padding:1px 5px;border-radius:4px}}"
        f"pre{{background:#161a21;border:1px solid #23272f;border-radius:8px;padding:12px;overflow:auto;max-height:420px}}"
        f".row{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#161a21;border:1px solid #23272f;border-radius:8px;padding:12px;flex:1;min-width:220px}}"
        f"form{{display:flex;gap:8px;margin:10px 0}}input,select,textarea{{background:#0f1115;color:#e6e6e6;border:1px solid #2a2f38;padding:7px 9px;border-radius:6px}}"
        f"button{{background:#2563eb;color:#fff;border:0;padding:7px 14px;border-radius:6px;cursor:pointer}}"
        f".danger{{background:#a33}}.ghost{{background:transparent;border:1px solid #2a2f38}}"
        f".bar{{float:right;font-size:12px;color:#8a93a3}}"
        f"</style></head><body><header><h1>🛡 Sovereign AI Worker</h1></header>"
        f"<div class=wrap>{body}</div></body></html>"
    )


# ---------------------------------------------------------------------------
# engine helper (same store + deterministic fallback as the CLI)
# ---------------------------------------------------------------------------


def _engine_for(ws, worker_name: str) -> WorkerEngine:
    from .inference import Inference

    worker = get_worker(worker_name, ws)
    store = WorkerStore(ws.state_dir)
    try:
        llm = Inference.from_env()
    except RuntimeError:
        llm = NullInference()
    return WorkerEngine(worker, store, inference=llm)


def _status_pill(status: str) -> str:
    cls = (
        "ok" if status == "SUCCESS"
        else "bad" if status in ("FAILED", "BLOCKED")
        else "pend" if status in ("AWAITING_APPROVAL", "INSUFFICIENT_EVIDENCE")
        else "warn"
    )
    return f"<span class='pill {cls}'>{_esc(status)}</span>"


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


def render_index(store: WorkerStore, ws, current_user: str = "", role: str = "") -> str:
    workers = list_workers(ws)
    runs = store.find("runs", order="seq", desc=True)[:25]
    wcards = "".join(
        f"<div class=card><b>{_esc(w.name)}</b><br><span style='color:#8a93a3'>{_esc(w.role)}</span><br>"
        f"tools: {_esc(', '.join(w.tools) or '(all)')}<br>"
        f"<span style='color:#8a93a3'>egress: {_esc(', '.join(w.egress_allow) or 'NONE (deny all)')}</span><br>"
        f"<span style='color:#8a93a3'>dlp: {_esc(', '.join(w.dlp_rules) or 'NONE (no payload scanning)')}</span></div>"
        for w in workers
    ) or "<i>no workers — run <code>python -m sworker init</code></i>"
    rrows = "".join(
        f"<tr><td>#{r['seq']}</td><td><code>{_esc(r['id'])}</code></td><td>{_esc(r['worker'])}</td>"
        f"<td>{_status_pill(r['status'])}</td><td>{_esc(r.get('summary', '')[:90])}</td>"
        f"<td><a href='/run?run_id={_esc(r['id'])}'>view</a></td></tr>"
        for r in runs
    ) or "<tr><td colspan=6><i>no runs yet</i></td></tr>"
    opts = "".join(f"<option>{_esc(w.name)}</option>" for w in workers)
    can_run = True  # caller gates; index always shows the form, submit enforces RBAC
    form = (
        "<form action='/run' method=post>"
        + f"<select name=worker>{opts}</select>"
        "<input name=request placeholder='request...' size=50 required>"
        "<button>Run</button></form>"
    ) if can_run else "<i>your role cannot start runs</i>"
    bar = f"<span class=bar>{_esc(current_user)} · role={_esc(role)} · <a href='/logout'>log out</a></span>" if current_user else ""
    return (
        f"{bar}"
        f"<h2>Workers</h2><div class=row>{wcards}</div>"
        f"<h2>New run</h2>{form}"
        f"<h2>Runs</h2>"
        f"<table><tr><th>#</th><th>id</th><th>worker</th><th>status</th><th>summary</th><th></th></tr>{rrows}</table>"
        f"<p style='color:#8a93a3'>JSON API: <a href='/api/runs'>/api/runs</a> · "
        f"<a href='/api/egress'>/api/egress</a> · "
        f"<a href='/api/dlp'>/api/dlp</a></p>"
    )


def render_sales(store: WorkerStore, ws, role: str = "", lead_id: str = "") -> str:
    """§71 — sales operating-system overview page (read-only view of the ledger)."""
    try:
        from .sales.repository import SalesRepository, default_ledger_path
        from .sales import metrics as sales_metrics
        from .sales import knowledge as sales_knowledge

        repo = SalesRepository(default_ledger_path())
        try:
            root = os.environ.get("DAILYSALESOS_ROOT", "")
            if not (root and os.path.isdir(root)):
                root = os.path.join(ws.root, "sales_knowledge")
                root = root if os.path.isdir(root) else ""
            targets = sales_knowledge.parse_daily_targets(root) if root else {}
            report = sales_metrics.daily_report(
                repo, targets=targets, targets_source=root or "", day=""
            )
            summary = repo.pipeline_summary()
            leads = repo.search_leads(limit=200)
        finally:
            repo.close()
    except Exception as e:  # pragma: no cover
        report, summary, leads = {"error": str(e)}, [], []
    # Lead list table (links to the per-lead detail view).
    lrows = "".join(
        f"<tr><td><a href='/sales/lead/{_esc(l['id'])}'>{_esc(l['id'])}</a></td>"
        f"<td>{_esc(l.get('company_name', ''))}</td>"
        f"<td>{_esc(l.get('industry', ''))}</td>"
        f"<td>{_esc(l.get('stage', ''))}</td>"
        f"<td>{l.get('score', 0)}</td></tr>"
        for l in leads
    ) or "<tr><td colspan=5><i>no leads</i></td></tr>"
    # Worker runs (sales workers only) — reuse the same store the dashboard reads.
    runs = [r for r in store.find("runs", order="seq", desc=True)
            if (r.get("worker") or "").startswith("sales_")][:25]
    rrows = "".join(
        f"<tr><td>#{r.get('seq', '')}</td><td><code>{_esc(r['id'])}</code></td>"
        f"<td>{_esc(r.get('worker', ''))}</td>"
        f"<td>{_status_pill(r.get('status', ''))}</td>"
        f"<td><a href='/run?run_id={_esc(r['id'])}'>view</a></td></tr>"
        for r in runs
    ) or "<tr><td colspan=5><i>no sales runs yet</i></td></tr>"
    if lead_id:
        return _render_sales_lead(store, ws, role, lead_id)
    plines = "".join(
        f"<tr><td>{_esc(s.get('stage', ''))}</td><td>{s.get('count', 0)}</td></tr>"
        for s in summary
    ) or "<tr><td colspan=2><i>no leads</i></td></tr>"
    vt = report.get("vs_target", {})
    vrows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{v.get('actual', 0)}</td><td>{v.get('target', 0)}</td>"
        f"<td>{'✓' if v.get('met') else '✗'}</td></tr>"
        for k, v in vt.items()
    ) or "<tr><td colspan=4><i>no targets loaded</i></td></tr>"
    failed = report.get("failed_sales_day")
    badge = "FAILED SALES DAY" if failed else ("OK" if failed is False else "—")
    return (
        f"<span class=bar>role={_esc(role)} · <a href='/'>home</a> · "
        f"<a href='/dashboard'>dashboard</a> · <a href='/procedures'>procedures</a></span>"
        f"<h2>Daily Sales OS — {_esc(report.get('date', ''))}</h2>"
        f"<p>Daily minimums: <b style='color:{'#e07' if failed else '#7c7'}'>{_esc(badge)}</b></p>"
        f"<h2>Pipeline by stage</h2><table><tr><th>stage</th><th>leads</th></tr>{plines}</table>"
        f"<h2>Activity vs targets</h2><table><tr><th>metric</th><th>actual</th><th>target</th><th>met</th></tr>{vrows}</table>"
        f"<h2>Leads ({len(leads)})</h2><table><tr><th>id</th><th>company</th><th>industry</th><th>stage</th><th>score</th></tr>{lrows}</table>"
        f"<h2>Sales worker runs</h2><table><tr><th>#</th><th>id</th><th>worker</th><th>status</th><th></th></tr>{rrows}</table>"
        f"<p style='color:#8a93a3'>JSON: <a href='/api/v1/sales/metrics'>/api/v1/sales/metrics</a> · "
        f"<a href='/api/v1/sales/pipeline'>/api/v1/sales/pipeline</a> · "
        f"<a href='/api/v1/sales/verify'>/api/v1/sales/verify</a></p>"
        f"<p style='color:#8a93a3'>Autonomous loop: <code>python -m sworker run sales_researcher \"execute DAILY_SALES_RUN\"</code></p>"
    )


def _render_sales_lead(store: WorkerStore, ws, role: str, lead_id: str) -> str:
    """Per-lead detail (read-only) — evidence + qualifications + drafts + history."""
    from .sales.repository import SalesRepository, default_ledger_path

    repo = SalesRepository(default_ledger_path())
    try:
        lead = repo.get_lead(lead_id)
        if not lead:
            return page("Lead not found", f"<p>No lead <code>{_esc(lead_id)}</code>.</p>"
                                      f"<p><a href='/sales'>back</a></p>")
        ld = lead.to_dict()
        ev = [e.to_dict() for e in repo.evidence_for(lead_id)]
        quals = [q.to_dict() for q in repo.qualifications_for(lead_id)]
        pp = [p.to_dict() for p in repo.pain_points_for(lead_id)]
        drafts = [d.to_dict() for d in repo.drafts(lead_id=lead_id)]
        hist = [h.to_dict() for h in repo.stage_history(lead_id)]
    finally:
        repo.close()
    ev_rows = "".join(
        f"<tr><td>{_esc(e.get('claim_type', ''))}</td><td>{_esc(str(e.get('claim_text', ''))[:80])}</td>"
        f"<td><code>{_esc(str(e.get('source_ref', '')))}</code></td><td>{_esc(e.get('tier', ''))}</td></tr>"
        for e in ev
    ) or "<tr><td colspan=4><i>no evidence</i></td></tr>"
    q_rows = "".join(
        f"<tr><td>{q.get('score', 0)}</td><td>{_esc(q.get('tier', ''))}</td><td>v{q.get('version', 0)}</td></tr>"
        for q in quals
    ) or "<tr><td colspan=3><i>not qualified</i></td></tr>"
    d_rows = "".join(
        f"<tr><td>{_esc(d.get('state', ''))}</td><td>{_esc(str(d.get('subject', ''))[:50])}</td></tr>"
        for d in drafts
    ) or "<tr><td colspan=2><i>no drafts</i></td></tr>"
    h_rows = "".join(
        f"<tr><td>{_esc(h.get('from_stage', ''))}</td><td>{_esc(h.get('to_stage', ''))}</td>"
        f"<td>{_esc(h.get('reason', ''))}</td></tr>"
        for h in hist
    ) or "<tr><td colspan=3><i>no stage history</i></td></tr>"
    return (
        f"<span class=bar><a href='/sales'>← all leads</a> · role={_esc(role)}</span>"
        f"<h2>Lead {_esc(lead_id)} — {_esc(ld.get('company_id', ''))}</h2>"
        f"<p>stage: <b>{_esc(str(ld.get('stage', '')))}</b> · score: {ld.get('score', 0)}</p>"
        f"<h2>Evidence ({len(ev)})</h2><table><tr><th>type</th><th>claim</th><th>source_ref</th><th>tier</th></tr>{ev_rows}</table>"
        f"<h2>Qualifications ({len(quals)})</h2><table><tr><th>score</th><th>tier</th><th>version</th></tr>{q_rows}</table>"
        f"<h2>Pain points ({len(pp)})</h2><pre>{_esc(str(pp))}</pre>"
        f"<h2>Drafts ({len(drafts)})</h2><table><tr><th>state</th><th>subject</th></tr>{d_rows}</table>"
        f"<h2>Stage history</h2><table><tr><th>from</th><th>to</th><th>reason</th></tr>{h_rows}</table>"
        f"<p style='color:#8a93a3'>JSON: <a href='/api/v1/sales/lead/{_esc(lead_id)}'>/api/v1/sales/lead/{_esc(lead_id)}</a></p>"
    )


def render_dashboard(store: WorkerStore, ws, role: str = "") -> str:
    """§27 — consolidated admin dashboard (no secrets surfaced)."""
    runs = store.find("runs", order="seq", desc=True)
    by_status: Dict[str, int] = {}
    for r in runs:
        s = r.get("status", "UNKNOWN")
        by_status[s] = by_status.get(s, 0) + 1
    workers = list_workers(ws)
    try:
        doc = _doctor.run_doctor(ws)
        health_rows = "".join(
            f"<tr><td>{_esc(c.get('severity', ''))}</td><td>{_esc(c.get('name', ''))}</td>"
            f"<td>{_esc(c.get('detail', ''))}</td></tr>"
            for c in doc.get("checks", [])
        ) or "<tr><td colspan=3><i>no checks</i></td></tr>"
    except Exception as e:  # pragma: no cover
        health_rows = f"<tr><td>error</td><td>doctor</td><td>{_esc(str(e))}</td></tr>"
    status_cells = "".join(
        f"<div class=card><b>{_esc(k)}</b><br>{v}</div>" for k, v in sorted(by_status.items())
    ) or "<i>no runs</i>"
    m = _metrics.snapshot()
    metric_cells = "".join(
        f"<div class=card><b>{_esc(k)}</b><br>{_esc(str(v))}</div>" for k, v in m.items()
    ) or "<i>no metrics</i>"
    wcards = "".join(
        f"<div class=card><b>{_esc(w.name)}</b><br><span style='color:#8a93a3'>{_esc(w.role)}</span>"
        f"<br>tools: {_esc(', '.join(w.tools) or '(all)')}</div>"
        for w in workers
    ) or "<i>no workers</i>"
    rrows = "".join(
        f"<tr><td>#{r['seq']}</td><td><code>{_esc(r['id'])}</code></td><td>{_esc(r['worker'])}</td>"
        f"<td>{_status_pill(r['status'])}</td>"
        f"<td><a href='/run?run_id={_esc(r['id'])}'>view</a></td></tr>"
        for r in runs[:25]
    ) or "<tr><td colspan=5><i>no runs yet</i></td></tr>"
    return (
        f"<span class=bar>role={_esc(role)} · <a href='/'>home</a> · <a href='/dashboard'>dashboard</a></span>"
        f"<h2>Workspace health</h2><table><tr><th>severity</th><th>check</th><th>detail</th></tr>{health_rows}</table>"
        f"<h2>Runs by status</h2><div class=row>{status_cells}</div>"
        f"<h2>Metrics</h2><div class=row>{metric_cells}</div>"
        f"<h2>Workers</h2><div class=row>{wcards}</div>"
        f"<h2>Recent runs</h2><table><tr><th>#</th><th>id</th><th>worker</th><th>status</th><th></th></tr>{rrows}</table>"
        f"<p style='color:#8a93a3'>JSON: <a href='/api/v1/dashboard'>/api/v1/dashboard</a></p>"
    )


def render_security(store: WorkerStore, ws, role: str = "") -> str:
    """§64 — security event feed (curated over the tamper-evident audit log)."""
    from .security_events import SecurityEvents

    sec = SecurityEvents(store)
    chain = store.verify_audit_chain()
    chain_ok = bool(chain.get("ok"))
    chain_pill = _status_pill("OK" if chain_ok else "BROKEN")
    counts = sec.counts_by_kind()
    count_cells = "".join(
        f"<div class=card><b>{_esc(k)}</b><br>{v}</div>" for k, v in sorted(counts.items())
    ) or "<i>no security events</i>"
    rows = "".join(
        f"<tr><td>{_sev_pill(e['severity'])}</td><td><code>{_esc(e['kind'])}</code></td>"
        f"<td>{_esc(e['label'])}</td><td>{_esc(e['summary'] or '')}</td>"
        f"<td>{_esc(str(e['ts'])[:19])}</td>"
        f"<td>{_esc(str(e['actor'] or ''))}</td></tr>"
        for e in sec.recent(limit=100)
    ) or "<tr><td colspan=6><i>no security events recorded</i></td></tr>"
    return (
        f"<span class=bar>role={_esc(role)} · <a href='/'>home</a> · "
        f"<a href='/dashboard'>dashboard</a> · <a href='/status'>status</a> · "
        f"<a href='/maturity'>maturity</a> · "
        f"<a href='/procedures'>procedures</a> · "
        f"<a href='/security'>security</a></span>"
        f"<h2>Audit chain integrity</h2><p>{chain_pill} "
        f"{chain.get('checked', 0)} of {chain.get('lines', 0)} lines verified "
        f"(hash-chained, tamper-evident)</p>"
        f"<h2>Security events by kind</h2><div class=row>{count_cells}</div>"
        f"<h2>Recent security events</h2>"
        f"<table><tr><th>sev</th><th>kind</th><th>event</th><th>summary</th>"
        f"<th>when</th><th>actor</th></tr>{rows}</table>"
        f"<p style='color:#8a93a3'>JSON: <a href='/api/v1/security'>/api/v1/security</a></p>"
    )


def _sev_pill(sev: str) -> str:
    color = {
        "info": "#3b82f6", "notice": "#22c55e", "warning": "#eab308",
        "critical": "#ef4444",
    }.get(sev, "#8a93a3")
    return f"<span class=pill style='background:{color}'>{_esc(sev)}</span>"


def _render_approvals(store, run_id: str) -> str:
    mgr = ApprovalManager(store)
    pending = mgr.pending(run_id)
    if not pending:
        return ""
    rows = "".join(
        f"<tr><td><code>{_esc(a['id'])}</code></td><td>{_esc(a['summary'])}</td>"
        f"<td><span class='pill'>{_esc(a['risk'])}</span></td>"
        f"<td><form action='/approve' method=post style='margin:0'>{_hidden('appr_id', a['id'])}"
        f"<button type=submit>Approve</button></form></td>"
        f"<td><form action='/deny' method=post style='margin:0'>{_hidden('appr_id', a['id'])}"
        f"<button type=submit class=danger>Reject</button></form></td></tr>"
        for a in pending
    )
    return (
        f"<h2>Pending approvals</h2><table>"
        f"<tr><th>id</th><th>summary</th><th>risk</th><th></th><th></th></tr>{rows}</table>"
    )


def _hidden(name: str, val: str) -> str:
    return f"<input type=hidden name={name} value={_esc(val)}>"


def render_run(store: WorkerStore, ws, run_id: str, role: str = "") -> str:
    run = store.get("runs", run_id)
    if not run:
        return "<h2>Run not found</h2><a href='/'>back</a>"
    steps = store.find("steps", run_id=run_id, order="idx")
    evs = store.find("evidence", run_id=run_id, order="created")
    arts = store.find("artifacts", run_id=run_id, order="created")
    audit = [e for e in store.iter_audit(run_id)]
    srows = "".join(
        f"<tr><td><span class='pill'>{_esc(s['status'])}</span></td><td>{_esc(s.get('description', ''))}</td>"
        f"<td><code>{_esc(s.get('tool', ''))}</code></td></tr>"
        for s in steps
    )
    erows = "".join(
        f"<tr><td>{_esc(e['provenance'])}</td><td>{_esc(e['summary'])}</td>"
        f"<td class=mono>{_esc(e.get('source_ref', ''))}</td></tr>"
        for e in evs
    )
    arows = "".join(
        f"<tr><td><a href='/?dl={_esc(a['path'])}'>{_esc(a['path'])}</a></td><td>{a.get('bytes', 0)} b</td></tr>"
        for a in arts
    )
    audit_txt = "\n".join(f"{_time(e['ts'])}  {e['event']:<22} {e['table']:<13} {e['id']}" for e in audit)
    verify_btn = (
        f"<form action='/verify' method=post style='display:inline'>{_hidden('run_id', run_id)}"
        f"<button type=submit>Run verification</button></form>"
    )
    if run["status"] in ("AWAITING_APPROVAL",):
        resume_btn = (
            f"<form action='/resume' method=post style='display:inline'>{_hidden('run_id', run_id)}"
            f"<button type=submit>Resume run</button></form>"
        )
    else:
        resume_btn = ""
    why_link = ""
    if run["status"] == "BLOCKED":
        why_link = (
            f"<p><a class='pill' style='background:#5a1d1d' href='/why?run_id={_esc(run_id)}'>"
            f"Why is this blocked? →</a></p>"
        )
    return (
        f"<h2>Run #{run['seq']} {_status_pill(run['status'])}</h2>"
        f"<p>{_esc(run.get('summary', ''))}</p>"
        f"<p style='color:#8a93a3'>worker: <b>{_esc(run['worker'])}</b> · intent: {_esc(run.get('intent', ''))} · "
        f"evidence: {len(evs)} · artifacts: {len(arts)} · "
        f"replay: <code>python -m sworker audit {_esc(run_id)}</code></p>"
        f"<p>{verify_btn} {resume_btn}</p>"
        f"{why_link}"
        f"{_render_approvals(store, run_id)}"
        f"<h2>Steps</h2><table>{srows}</table>"
        f"<h2>Evidence</h2><table>{erows or '<tr><td><i>none</i></td></tr>'}</table>"
        f"<h2>Artifacts</h2><table>{arows or '<tr><td><i>none</i></td></tr>'}</table>"
        f"<h2>Audit trail ({len(audit)} events)</h2><pre class=mono>{_esc(audit_txt)}</pre>"
        f"<p><a href='/'>← back</a></p>"
    )


def render_inspect(store: WorkerStore, ws, run_id: str, role: str = "") -> str:
    """§39 — concise execution timeline for a run, inspectable by design.

    Mirrors ``cmd_inspect``: RUN -> ACTION -> TOOL -> OBSERVATION -> EVIDENCE ->
    VERIFICATION -> ARTIFACT -> APPROVAL in one ordered view.
    """
    run = store.get("runs", run_id)
    if not run:
        return "<h2>Run not found</h2><a href='/'>back</a>"
    rid = run["id"]
    steps = store.find("steps", run_id=rid, order="created")
    actions = store.find("actions", run_id=rid, order="created")
    obss = store.find("observations", run_id=rid, order="created")
    evs = store.find("evidence", run_id=rid, order="created")
    vers = store.find("verifications", run_id=rid, order="created")
    arts = store.find("artifacts", run_id=rid, order="created")
    apps = store.find("approvals", run_id=rid, order="created")

    tl: List[tuple] = []
    for s in steps:
        tl.append((s.get("created", 0), "STEP",
                   f"[{_esc(s.get('status', ''))}] {_esc(s.get('description', '')[:70])}"))
    for a in actions:
        tl.append((a.get("created", 0), "ACTION",
                   f"{_esc(a.get('tool', ''))} [{_esc(a.get('risk', ''))}] {_esc(a.get('status', ''))}"))
    for o in obss:
        flag = "ok" if o.get("ok") else "FAIL"
        tl.append((o.get("created", 0), "OBSERVATION", f"{flag} {_esc(o.get('output', '')[:70])}"))
    for e in evs:
        tl.append((e.get("created", 0), "EVIDENCE",
                   f"({_esc(e.get('provenance', ''))}) {_esc(e.get('summary', '')[:70])} <span class=mono>{_esc(e.get('source_ref', ''))}</span>"))
    for v in vers:
        tl.append((v.get("created", 0), "VERIFY", f"{_esc(v.get('check', ''))} {_esc(v.get('outcome', ''))}"))
    for a in arts:
        tl.append((a.get("created", 0), "ARTIFACT",
                   f"{_esc(a.get('title', '') or a.get('kind', ''))} <span class=mono>{_esc(a.get('path', ''))}</span>"))
    for a in apps:
        tl.append((a.get("created", 0), "APPROVAL",
                   f"[{_esc(a.get('risk', ''))}] {_esc(a.get('state', ''))} {_esc(a.get('summary', '')[:60])}"))
    tl.sort(key=lambda t: t[0])
    rows = "".join(
        f"<tr><td class='num'>{i:02d}</td><td><span class='pill'>{_esc(k)}</span></td><td>{t}</td></tr>"
        for i, (_, k, t) in enumerate(tl, 1)
    )
    return (
        f"<h2>Inspect Run #{run['seq']} {_status_pill(run['status'])}</h2>"
        f"<p style='color:#8a93a3'>worker: <b>{_esc(run['worker'])}</b> · "
        f"intent: {_esc(run.get('intent', ''))}</p>"
        f"<p>JSON: <code>python -m sworker inspect {_esc(rid)} --json</code></p>"
        f"<table class=timeline>{rows}</table>"
        f"<p><a href='/run?run_id={_esc(rid)}'>full run view →</a> · <a href='/'>← back</a></p>"
    )


def render_why(store: WorkerStore, ws, run_id: str, role: str = "") -> str:
    """§65 — HTML view of 'why is this run (or the workspace) blocked?'."""
    from .block_explainer import BlockExplainer, explain_blocked

    out = explain_blocked(store, run_id) if run_id else BlockExplainer(store).explain_workspace()
    rows = "".join(
        f"<tr><td><span class='pill'>{_esc(r['severity'])}</span></td>"
        f"<td><code>{_esc(r['source'])}/{_esc(r['kind'])}</code></td>"
        f"<td>{_esc(r['reason'])}</td>"
        f"<td>{_esc(r.get('mitigation', ''))}</td></tr>"
        for r in out["reasons"]
    ) or "<tr><td colspan=4><i>no block reasons recorded</i></td></tr>"
    back = f"<a href='/run?run_id={_esc(run_id)}'>← run</a>" if run_id else "<a href='/'>← dashboard</a>"
    return (
        f"<h2>Why blocked? {_status_pill(out['status'] or 'n/a')}</h2>"
        f"<p>{_esc(out['summary'])}</p>"
        f"<table class=mono><tr><th>severity</th><th>source</th><th>reason</th><th>fix</th></tr>{rows}</table>"
        f"<p>{back}</p>"
    )
    run = store.get("runs", run_id)
    if not run:
        return "<h2>Run not found</h2><a href='/'>back</a>"
    worker = get_worker(run["worker"], ws)


def render_verify(store: WorkerStore, ws, run_id: str) -> str:
    run = store.get("runs", run_id)
    if not run:
        return "<h2>Run not found</h2><a href='/'>back</a>"
    worker = get_worker(run["worker"], ws)
    eng = _engine_for(ws, run["worker"])
    proc = load_procedure(worker, run["procedure"]) if run.get("procedure") else None
    specs = (proc and procedure_verifications(proc, {})) or run.get("verifications") or []
    if not specs:
        return (
            f"<h2>Verification</h2><p>no verification checks declared for run {_esc(run_id)}.</p>"
            f"<p><a href='/run?run_id={_esc(run_id)}'>← back to run</a></p>"
        )
    rows = []
    any_fail = False
    for raw_spec in specs:
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        v = eng.record_verification(run, spec, eng._tool_ctx(run))
        flag = v.outcome.value
        if flag == "FAIL":
            any_fail = True
        cls = "ok" if flag == "PASS" else ("bad" if flag == "FAIL" else "warn")
        rows.append(
            f"<tr><td><span class='pill {cls}'>{_esc(flag)}</span></td>"
            f"<td><code>{_esc(v.check)}</code></td><td>{_esc(v.detail)}</td>"
            f"<td>{_esc(str(v.actual))}</td></tr>"
        )
    head = "FAILED" if any_fail else "ALL PASSED"
    return (
        f"<h2>Verification — {_esc(head)}</h2>"
        f"<table><tr><th>result</th><th>check</th><th>detail</th><th>actual</th></tr>{''.join(rows)}</table>"
        f"<p><a href='/run?run_id={_esc(run_id)}'>← back to run</a></p>"
    )


def render_status(store, ws, role=""):
    """§66 — compose every hardening control into one fail-closed verdict."""
    from .system_status import SystemStatus

    out = SystemStatus(store).compose()
    sev_cls = {
        "ok": "ok", "warning": "warn", "unknown": "bad", "critical": "bad",
    }
    rows = []
    for c in out["controls"]:
        cls = sev_cls.get(c["severity"], "bad")
        rows.append(
            f"<tr><td><span class='pill {cls}'>{_esc(c['severity'])}</span></td>"
            f"<td><code>{_esc(c['name'])}</code></td>"
            f"<td>{_esc(c['status'])}</td>"
            f"<td style='color:#67707f'><code>{_esc(c['source'])}</code></td></tr>"
        )
    return (
        f"<h2>System status — <span class='pill {sev_cls.get(out['verdict'], 'bad')}'>"
        f"{_esc(out['verdict'].upper())}</span></h2>"
        f"<p style='color:#8a93a3'>Worst-severity-wins across every hardening control. "
        f"Each control reads only its subsystem's real state.</p>"
        f"<table><tr><th>severity</th><th>control</th><th>status</th><th>source</th></tr>"
        f"{''.join(rows)}</table>"
        f"<p><a href='/security'>security events →</a> · "
        f"<a href='/why?workspace=1'>why blocked? →</a></p>"
    )


def render_maturity(store, ws, role=""):
    """§70 — score the deployment's hardening posture from real state."""
    from .maturity import MaturityModel

    rep = MaturityModel(store, os.path.basename(str(getattr(ws, "root", "")))).assess()
    tier_cls = {0: "bad", 1: "bad", 2: "warn", 3: "ok", 4: "ok"}
    rows = []
    for d in rep.dimensions:
        cls = tier_cls.get(d.tier, "bad")
        rows.append(
            f"<tr><td><span class='pill {cls}'>{_esc(d.tier_name)}</span></td>"
            f"<td>{_esc(d.label)}</td>"
            f"<td style='color:#67707f'>{_esc(d.evidence)}</td>"
            f"<td style='color:#67707f'>{_esc(d.recommendation or '—')}</td></tr>"
        )
    return (
        f"<h2>Maturity — <span class='pill {tier_cls.get(rep.floor, 'bad')}'>"
        f"{_esc(rep.level.upper())}</span></h2>"
        f"<p style='color:#8a93a3'>Weakest-link scoring: the platform's level is the "
        f"<b>lowest</b> of every dimension (a strong audit chain cannot mask a missing "
        f"auth layer). Every signal reads only real, persisted state.</p>"
        f"<p>{_esc(rep.summary)}</p>"
        f"<table><tr><th>level</th><th>dimension</th><th>evidence</th><th>next step</th></tr>"
        f"{''.join(rows)}</table>"
        f"<p><a href='/status'>system status →</a> · "
        f"<a href='/security'>security events →</a></p>"
    )


def render_procedures(store, ws, role=""):
    """§23 — list every worker's published procedure versions (reviewed, frozen).

    Surfaces only the published (reviewed) registry: names, versions, current
    pin, author, and content hash. The procedure *bodies* are never inlined
    here (fetch a specific version via the API if needed) — this is the review
    ledger, not the editor.
    """
    from . import procedures as P
    from .config import list_workers

    rows = []
    for w in list_workers(ws):
        published = P.list_published(w)
        cur = {p["name"]: P.current_version(w, p["name"]) for p in published}
        if not published:
            rows.append(
                f"<tr><td><code>{_esc(w.name)}</code></td><td colspan=4 "
                f"style='color:#67707f'>no published procedures</td></tr>"
            )
            continue
        for p in published:
            is_cur = cur[p["name"]] == p["version"]
            pin = "<span class='pill ok'>current</span>" if is_cur else "<span style='color:#67707f'>—</span>"
            rows.append(
                f"<tr><td><code>{_esc(w.name)}</code></td>"
                f"<td><code>{_esc(p['name'])}</code></td>"
                f"<td>v{_esc(p['version'])}</td>"
                f"<td>{pin}</td>"
                f"<td style='color:#67707f'>{_esc(p['author'] or '(unknown)')} "
                f"· <code>{_esc(p['hash'][:12])}</code></td></tr>"
            )
    return (
        f"<h2>Published procedures</h2>"
        f"<p style='color:#8a93a3'>Review ledger of frozen, versioned procedure "
        f"releases per worker. Publish/rollback are RBAC-gated "
        f"(<code>procedure:publish</code>) and run via the CLI; the JSON mirror is "
        f"<a href='/api/v1/procedures'>/api/v1/procedures</a>.</p>"
        f"<table><tr><th>worker</th><th>procedure</th><th>version</th>"
        f"<th>pin</th><th>author / hash</th></tr>{''.join(rows)}</table>"
        f"<p><a href='/maturity'>maturity →</a> · "
        f"<a href='/security'>security events →</a></p>"
    )


def _procedures_payload(handler):
    """§23 — shared JSON for /api/v1/procedures (worker → published versions)."""
    from . import procedures as P
    from .config import list_workers

    out = []
    for w in list_workers(handler._ws):
        published = P.list_published(w)
        current = {p["name"]: P.current_version(w, p["name"]) for p in published}
        for p in published:
            out.append({
                "worker": w.name,
                "name": p["name"],
                "version": p["version"],
                "current": current[p["name"]] == p["version"],
                "author": p["author"],
                "hash": p["hash"],
            })
    return {"procedures": out}, 200


def render_login(error: str = ""):
    err = f"<p class=bad>{_esc(error)}</p>" if error else ""
    return (
        f"{err}"
        f"<h2>Sign in</h2>"
        f"<form action='/login' method=post>"
        f"<input name=username placeholder='username' required>"
        f"<input name=password type=password placeholder='password' required>"
        f"<button>Sign in</button></form>"
        f"<p style='color:#8a93a3'>Local-only. Create an account with "
        f"<code>python -m sworker user add &lt;name&gt; --password ... --role operator</code>.</p>"
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Permission required for each mutating route (server-side, fail-closed).
_ROUTE_PERMS = {
    "/run": "run:create",
    "/approve": "approval:decide",
    "/deny": "approval:decide",
    "/resume": "run:create",
    "/verify": "run:read",
}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *a, store: "WorkerStore", ws, auth: "AuthProvider", rbac: "RBAC", port: int = 8777, **k):
        self._store = store
        self._ws = ws
        self._auth = auth
        self._rbac = rbac
        self._port = port
        super().__init__(*a, **k)

    def log_message(self, fmt, *args):  # type: ignore[override]
        pass

    def _send(self, body: "str | bytes", ctype="text/html; charset=utf-8", code=200,
              headers: "dict | None" = None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, code: int = 200):
        """§36 — JSON response with hardening headers (CSP, nosniff). Never reflects
        secrets; callers are responsible for not putting them in ``obj``."""
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def _form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        data = parse_qs(raw.decode("utf-8", "replace"))
        return {k: (v[0] if v else "") for k, v in data.items()}

    def _cookie_user(self) -> "tuple[str, str]":
        """(username, role) for the session cookie, or ('', '') if none/invalid."""
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("sworker_session="):
                token = part.split("=", 1)[1]
        if not token:
            return "", ""
        username = self._auth.validate_session(token)
        if not username:
            return "", ""
        u = self._auth.get_user(username)
        return username, (u.role if u else "viewer")

    def _origin_ok(self) -> bool:
        """Same-origin check. Empty Origin/Referer (browser-less, curl) is allowed;
        a cross-origin value is rejected. Loopback-only by design."""
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        allowed = {"", f"http://127.0.0.1:{self._port}/", f"http://127.0.0.1:{self._port}"}
        for hdr in (origin, referer):
            if hdr and hdr not in allowed:
                return False
        return True

    def _require_auth(self) -> "tuple[str, str] | None":
        """Return (username, role) if authenticated, else write 401 + redirect
        to /login and return None."""
        user, role = self._cookie_user()
        if not user:
            body = page("Sign in", render_login("Session required — please sign in."))
            self._send(body, code=401, headers={"Location": "/login"})
            return None
        return user, role

    def _require_perm(self, user: str, role: str, perm: str) -> bool:
        if self._rbac.authorize(role, perm):
            return True
        self._send(
            page("Forbidden", f"<p>User <b>{_esc(user)}</b> (role {_esc(role)}) "
                  f"lacks permission <code>{_esc(perm)}</code>.</p>").encode(),
            code=403,
        )
        return False

    def _gate_mutate(self, form: dict) -> "tuple[str, str] | None":
        """Authenticate + RBAC + CSRF for a state-changing request. Returns
        (user, role) or None (and has written the error response)."""
        if not self._origin_ok():
            self._send(page("Forbidden", "<p>Cross-origin request rejected (CSRF defense).</p>").encode(), code=403)
            return None
        auth = self._require_auth()
        if auth is None:
            return None
        user, role = auth
        perm = _ROUTE_PERMS.get(urlparse(self.path).path)
        if perm and not self._require_perm(user, role, perm):
            return None
        return user, role

    def do_GET(self):
        url = urlparse(self.path)
        qs = self._qs()
        # login + logout are reachable without a session
        if url.path == "/login":
            self._send(page("Sign in", render_login()))
            return
        if url.path == "/logout":
            self._do_logout()
            return
        auth = self._require_auth()
        if auth is None:
            return
        user, role = auth
        try:
            if url.path == "/run" and qs.get("run_id"):
                self._send(page("Run", render_run(self._store, self._ws, qs["run_id"][0], role=role)))
            elif url.path == "/why" and qs.get("run_id"):  # §65
                self._send(page("Why blocked?", render_why(self._store, self._ws, qs["run_id"][0], role=role)))
            elif url.path == "/inspect" and qs.get("run_id"):  # §39
                self._send(page("Inspect", render_inspect(self._store, self._ws, qs["run_id"][0], role=role)))
            elif url.path == "/verify" and qs.get("run_id"):
                self._send(page("Verify", render_verify(self._store, self._ws, qs["run_id"][0])))
            elif url.path == "/api/runs":
                self._send(
                    json.dumps(self._store.find("runs", order="seq", desc=True)[:50]).encode(),
                    "application/json",
                )
            elif url.path == "/api/egress":
                self._send(
                    json.dumps(render_egress_log(self._store)).encode(),
                    "application/json",
                )
            elif url.path == "/api/dlp":
                self._send(
                    json.dumps(render_dlp_log(self._store)).encode(),
                    "application/json",
                )
            elif url.path == "/dashboard":
                self._send(page("Dashboard", render_dashboard(self._store, self._ws, role)))
            elif url.path == "/security":  # §64
                self._send(page("Security", render_security(self._store, self._ws, role)))
            elif url.path == "/status":  # §66
                self._send(page("System status", render_status(self._store, self._ws, role)))
            elif url.path == "/maturity":  # §70
                self._send(page("Maturity", render_maturity(self._store, self._ws, role)))
            elif url.path == "/procedures":  # §23
                self._send(page("Procedures", render_procedures(self._store, self._ws, role)))
            elif url.path == "/sales":  # §71
                self._send(page("Daily Sales OS", render_sales(self._store, self._ws, role)))
            elif url.path.startswith("/sales/lead/"):  # §71 per-lead detail
                lid = url.path[len("/sales/lead/"):]
                self._send(page(f"Lead {lid}", render_sales(self._store, self._ws, role, lead_id=lid)))
            # --- §37 /api/v1 (versioned, hardened JSON) -----------------------------
            elif url.path == "/api/v1/openapi.json":
                self._send_json(openapi_doc())
            elif url.path.startswith("/api/v1/"):
                body, code = _api_v1_dispatch(self, url, qs)
                self._send_json(body, code=code)
            else:
                self._send(page("Sovereign AI Worker", render_index(self._store, self._ws, user, role)))
        except Exception:  # pragma: no cover
            self._send(page("Error", f"<pre class=mono>{_esc(traceback.format_exc())}</pre>").encode(), code=500)

    def _do_logout(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("sworker_session="):
                self._auth.revoke_session(part.split("=", 1)[1])
        self._send(
            page("Signed out", "<p>You have been signed out. <a href='/login'>Sign in</a></p>").encode(),
            code=303, headers={"Location": "/login", "Set-Cookie": "sworker_session=; Max-Age=0; Path=/"},
        )

    def do_POST(self):
        url = urlparse(self.path)
        form = self._form()
        if url.path == "/login":
            self._do_login(form)
            return
        if url.path == "/logout":
            self._do_logout()
            return
        auth = self._gate_mutate(form)
        if auth is None:
            return
        user, role = auth
        try:
            if url.path == "/api/v1/explain":
                if not self._require_perm(user, role, "run:read"):
                    return
                worker = form.get("worker", "")
                request = form.get("request", "").strip()
                if not worker or not request:
                    self._send_json({"error": "worker + request required"}, code=400)
                    return
                from .config import get_worker, Workspace
                from .engine import WorkerEngine, WorkerStore
                try:
                    wc = get_worker(worker, self._ws)
                except FileNotFoundError as e:
                    self._send_json({"error": str(e)}, code=404)
                    return
                eng = WorkerEngine(wc, WorkerStore(self._ws.state_dir))
                try:
                    res = _explain.explain(eng, request, procedure=form.get("procedure", "") or "")
                except RuntimeError as e:
                    self._send_json({"error": str(e)}, code=400)
                    return
                self._send_json(res.to_dict())
                return
            if url.path == "/api/v1/safemode":
                if not self._require_perm(user, role, "admin"):
                    return
                from .safemode import SafeMode, READONLY, LOCKED, OFF

                level = (form.get("level") or "").strip().lower()
                sm = SafeMode(self._store)
                if level in ("on", READONLY):
                    lv = sm.enable()
                elif level == "off":
                    lv = sm.disable()
                elif level == "readonly":
                    lv = sm.set_level(READONLY)
                elif level == "locked":
                    lv = sm.lock()
                else:
                    self._send_json({"error": f"unknown level {level!r}"}, code=400)
                    return
                self._send_json({"ok": True, "level": lv})
                return
            if url.path == "/api/v1/incident":
                if not self._require_perm(user, role, "admin"):
                    return
                from .incident import IncidentLedger

                led = IncidentLedger(self._store)
                action = (form.get("action") or "status").strip().lower()
                summary = (form.get("summary") or "incident via web").strip()
                by = (form.get("by") or getattr(user, "username", "admin"))
                if action in ("open",):
                    try:
                        res = led.open(summary, by=by)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, code=409)
                        return
                    self._send_json({"ok": True, "state": res["state"], "safe_mode": res["safe_mode"]})
                    return
                if action == "lockdown":
                    res = led.lockdown(summary, by=by)
                    self._send_json({"ok": True, "state": res["state"], "safe_mode": res["safe_mode"]})
                    return
                if action == "close":
                    res = led.close(by=by, note=form.get("note", ""))
                    self._send_json({"ok": True, "state": res["state"], "safe_mode": res["safe_mode"], "changed": res["changed"]})
                    return
                self._send_json(led.status_dict(), 200)
                return
            elif url.path == "/run":
                worker = form.get("worker", "")
                request = form.get("request", "").strip()
                if not worker or not request:
                    self._send(page("Error", "<p>worker + request required</p>").encode(), code=400)
                    return
                eng = _engine_for(self._ws, worker)
                res = eng.run(request, on_event=lambda e, p: None)
                self._redirect(f"/run?run_id={res.run.id}")
            elif url.path == "/approve":
                user, role = self._cookie_user()
                self._decide(form.get("appr_id", ""), True, user, role)
            elif url.path == "/deny":
                user, role = self._cookie_user()
                self._decide(form.get("appr_id", ""), False, user, role)
            elif url.path == "/resume":
                run_id = form.get("run_id", "")
                run = self._store.get("runs", run_id)
                if not run:
                    self._send(page("Error", "<p>no such run</p>").encode(), code=404)
                    return
                eng = _engine_for(self._ws, run["worker"])
                res = eng.run("", resume_run_id=run_id, on_event=lambda e, p: None)
                self._redirect(f"/run?run_id={res.run.id}")
            elif url.path == "/verify":
                run_id = form.get("run_id", "")
                self._redirect(f"/verify?run_id={run_id}")
            else:
                self._send(page("Error", "<p>unknown action</p>").encode(), code=404)
        except Exception:  # pragma: no cover
            self._send(page("Error", f"<pre class=mono>{_esc(traceback.format_exc())}</pre>").encode(), code=500)

    def _do_login(self, form: dict) -> None:
        username = form.get("username", "")
        password = form.get("password", "")
        sess = self._auth.authenticate(username, password)
        if sess is None:
            self._send(page("Sign in", render_login("Invalid username or password.")).encode(), code=401)
            return
        self._send(
            page("Redirecting", "<p><a href='/'>continue</a></p>").encode(),
            code=303,
            headers={
                "Location": "/",
                "Set-Cookie": f"sworker_session={sess.token}; HttpOnly; SameSite=Strict; Path=/",
            },
        )

    def _decide(self, appr_id: str, approved: bool, by: str = "web", role: str = ""):
        if not appr_id:
            self._send(page("Error", "<p>missing approval id</p>").encode(), code=400)
            return
        mgr = ApprovalManager(self._store)
        try:
            rec = mgr.decide(appr_id, approved=approved, by=by or "web", role=role, note="via web UI")
        except KeyError:
            self._send(page("Error", f"<p>no pending approval {appr_id!r}</p>").encode(), code=404)
            return
        except ApprovalError as exc:
            # e.g. the voter's role is below min_role, or quorum unmet-closed
            self._send(page("Forbidden", f"<p>{_esc(str(exc))}</p>").encode(), code=403)
            return
        self._redirect(f"/run?run_id={rec['run_id']}")

    def _redirect(self, loc: str):
        body = page("Redirecting", f"<p><a href='{_esc(loc)}'>continue</a></p>")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _dashboard_payload(handler: "Handler") -> Dict[str, Any]:
    """§27 — consolidated admin dashboard view (fail-closed: no secrets surfaced)."""
    store = handler._store
    ws = handler._ws
    runs = store.find("runs", order="seq", desc=True)
    by_status: Dict[str, int] = {}
    for r in runs:
        by_status[r.get("status", "UNKNOWN")] = by_status.get(r.get("status", "UNKNOWN"), 0) + 1
    pending = sum(1 for r in runs if r.get("status") == "AWAITING_APPROVAL")
    try:
        health = _doctor.run_doctor(ws)
    except Exception:
        health = {"ok": False, "checks": []}
    return {
        "workspace": ws.root if hasattr(ws, "root") else str(ws),
        "health": health,
        "workers": [w.name for w in list_workers(ws)],
        "runs_total": len(runs),
        "runs_by_status": by_status,
        "pending_approvals": pending,
        "metrics": _metrics.snapshot(),
    }


def _security_payload(handler: "Handler") -> Dict[str, Any]:
    """§64 — curated security-event feed + audit-chain integrity verdict."""
    from .security_events import SecurityEvents

    sec = SecurityEvents(handler._store)
    events = sec.recent(limit=100)
    chain = handler._store.verify_audit_chain()
    return {
        "audit_chain_ok": bool(chain.get("ok")),
        "audit_lines": chain.get("lines", 0),
        "audit_checked": chain.get("checked", 0),
        "counts_by_kind": sec.counts_by_kind(),
        "events": events,
    }


def openapi_doc() -> Dict[str, Any]:
    """§37 — minimal OpenAPI 3.0 doc for the /api/v1 surface (self-describing)."""
    paths = {}
    for p in ("/health", "/workers", "/runs", "/metrics"):
        paths[p] = {"get": {"summary": f"{p} endpoint", "responses": {"200": {"description": "ok"}}}}
    paths["/runs/{{run_id}}"] = {"get": {"summary": "run detail", "responses": {"200": {"description": "ok"}}}}
    paths["/explain"] = {"post": {"summary": "dry-run explanation (§29)", "responses": {"200": {"description": "ok"}}}}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Sovereign AI Worker API",
            "version": "1.0.0",
            "description": "Local-first worker control plane. All routes require an authenticated session cookie (set via /login).",
        },
        "servers": [{"url": "/api/v1"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "sworker_session"}
            }
        },
        "security": [{"sessionCookie": []}],
    }


def _api_v1_dispatch(handler: "Handler", url: "Any", qs: "dict") -> "tuple[Any, int]":
    """§37 — route /api/v1/* to a handler, returning (body, status)."""
    path = url.path[len("/api/v1"):]  # strip prefix; leading "/" remains
    store = handler._store
    if path.startswith("/sales"):
        from .sales import web as sales_web

        return sales_web.dispatch(path[len("/sales"):], qs)
    if path == "/health" or path == "":
        return _doctor.run_doctor(handler._ws), 200
    if path == "/workers":
        return [w.name for w in list_workers(handler._ws)], 200
    if path == "/runs":
        return store.find("runs", order="seq", desc=True)[:50], 200
    if path.startswith("/runs/"):
        rid = path[len("/runs/"):]
        run = store.get("runs", rid)
        if not run:
            return {"error": "run not found"}, 404
        return run, 200
    if path.startswith("/inspect/"):  # §39
        rid = path[len("/inspect/"):]
        run = store.get("runs", rid)
        if not run:
            return {"error": "run not found"}, 404
        # Same timeline the CLI emits; built directly from store records.
        def _timeline():
            tl = []
            for s in store.find("steps", run_id=rid, order="created"):
                tl.append((s.get("created", 0), "STEP", f"[{s.get('status','')}] {s.get('description','')[:70]}"))
            for a in store.find("actions", run_id=rid, order="created"):
                tl.append((a.get("created", 0), "ACTION", f"{a.get('tool','')} [{a.get('risk','')}] {a.get('status','')}"))
            for o in store.find("observations", run_id=rid, order="created"):
                tl.append((o.get("created", 0), "OBSERVATION", f"{'ok' if o.get('ok') else 'FAIL'} {o.get('output','')[:70]}"))
            for e in store.find("evidence", run_id=rid, order="created"):
                tl.append((e.get("created", 0), "EVIDENCE", f"({e.get('provenance','')}) {e.get('summary','')[:70]} src={e.get('source_ref','')}"))
            for v in store.find("verifications", run_id=rid, order="created"):
                tl.append((v.get("created", 0), "VERIFY", f"{v.get('check','')} {v.get('outcome','')}"))
            for a in store.find("artifacts", run_id=rid, order="created"):
                tl.append((a.get("created", 0), "ARTIFACT", f"{a.get('title','') or a.get('kind','')} {a.get('path','')}"))
            for a in store.find("approvals", run_id=rid, order="created"):
                tl.append((a.get("created", 0), "APPROVAL", f"[{a.get('risk','')}] {a.get('state','')} {a.get('summary','')[:60]}"))
            tl.sort(key=lambda t: t[0])
            return [{"kind": k, "text": t} for _, k, t in tl]
        return {
            "id": rid, "seq": run.get("seq"), "worker": run["worker"],
            "status": run["status"], "intent": run.get("intent", ""),
            "summary": run.get("summary", ""), "timeline": _timeline(),
        }, 200
    if path == "/metrics":
        return _metrics.snapshot(), 200
    if path == "/dashboard":
        return _dashboard_payload(handler), 200
    if path == "/security":  # §64
        return _security_payload(handler), 200
    if path == "/status":  # §66
        from .system_status import SystemStatus

        return SystemStatus(store).compose(), 200
    if path == "/maturity":  # §70
        from .maturity import MaturityModel

        label = os.path.basename(str(handler._ws.root))
        return MaturityModel(store, label).assess().to_dict(), 200
    if path == "/safemode":
        from .safemode import SafeMode

        return SafeMode(store).status_dict(), 200
    if path == "/procedures":  # §23
        return _procedures_payload(handler)
    if path == "/why":  # §65
        from .block_explainer import BlockExplainer, explain_blocked

        rid = (qs.get("run_id") or [None])[0]
        if rid:
            return explain_blocked(store, rid), 200
        return BlockExplainer(store).explain_workspace(), 200
    if path == "/incident":
        from .incident import IncidentLedger

        return IncidentLedger(store).status_dict(), 200
    return {"error": "not found", "path": path}, 404


def serve(port: int = 8777, home: str = "", token: str = "", host: str = "127.0.0.1") -> None:
    if home:
        os.environ["SWORKER_HOME"] = os.path.abspath(home)
    ws = default_workspace()
    ws.ensure()
    store = WorkerStore(ws.state_dir)
    auth = AuthProvider(store)
    rbac = RBAC()
    httpd = ThreadingHTTPServer(
        (host, port),
        lambda *a, **k: Handler(*a, store=store, ws=ws, auth=auth, rbac=rbac, port=port, **k),
    )
    print(f"Sovereign AI Worker UI on http://{host}:{port}  (Ctrl-C to stop)")
    print("(the static startup token is removed; sign in with a local account)")
    print("  create one: python -m sworker user add <name> --password ... --role operator")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def cli(argv=None):
    ap = argparse.ArgumentParser(prog="sworker.web")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--home", default="")
    ap.add_argument(
        "--token",
        default="",
        help="DEPRECATED/ignored — auth is now by local account login",
    )
    args = ap.parse_args(argv)
    serve(port=args.port, home=args.home, token=args.token)


if __name__ == "__main__":
    cli()
