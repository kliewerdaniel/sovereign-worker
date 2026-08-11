"""The Worker execution engine.

Implements the lifecycle literally:

    REQUEST -> INTENT -> PLAN -> ACTION SELECTION -> TOOL EXECUTION ->
    OBSERVATION -> EVIDENCE -> VERIFICATION -> ARTIFACT -> APPROVAL ->
    FINAL EXECUTION -> AUDIT

Design commitments:

* The model proposes; the engine disposes. Every tool call goes through
  ``PermissionEngine.evaluate`` before it runs, and the model cannot see or
  alter that decision.
* Nothing is fabricated. If the model is unreachable, the engine falls back to a
  deterministic plan rather than inventing content. If a tool fails, the step
  fails and is recorded as failed.
* Evidence is minted only from real observations (``EvidenceLedger``).
* Every state transition is an event in the append-only ledger, so a run can be
  reconstructed byte-for-byte from ``store.events(run_id)``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .approvals import ApprovalManager
from .config import WorkerConfig
from .degradation import (
    DegradationLedger,
    MODEL_FALLBACK,
    KNOWLEDGE_UNCOMPILED,
    CRITICAL,
    INCIDENT_ACTIVE,
)
from .evidence import EvidenceLedger
from .inference import Inference, NullInference
from .models import (
    Action,
    ActionStatus,
    Artifact,
    Claim,
    Observation,
    Plan,
    Provenance,
    Run,
    RunStatus,
    Step,
    StepStatus,
    Task,
    Verification,
    VerificationOutcome,
    now,
)
from .permissions import DecompositionGuard, PermissionEngine
from .safemode import SafeMode, SAFE_MODE_BLOCK
from .store import WorkerStore
from .tools import ToolContext, ToolError, ToolRegistry, build_registry
from . import verify as verify_mod
from .connectors import ConnectorManager, ConnectorError
from .injection import scan_dict as _scan_injection

MAX_STEPS = 24


PLANNER_SYSTEM = """You are the planning module of a local-first AI worker. \
You do NOT execute anything: you emit a plan, and a separate deterministic engine \
decides what is permitted to run.

Return ONLY a JSON object:
{"intent": "<one sentence>", "steps": [{"description": "...", "tool": "<tool name or null>", \
"args": {...}, "why": "..."}]}

Rules:
- Use ONLY the listed tools with their exact argument names.
- Prefer data.query for any number you intend to state. Never state a figure you did not compute.
- Read knowledge with knowledge.search before asserting company policy.
- Finish by writing an artifact with fs.write when the task asks for a report.
- Keep the plan under 12 steps."""


@dataclass
class RunResult:
    run: Run
    status: RunStatus
    summary: str
    artifacts: List[Artifact]
    claims: List[Claim]
    pending_approvals: List[Dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.status in (RunStatus.SUCCESS, RunStatus.PARTIAL_SUCCESS)


class WorkerEngine:
    def __init__(
        self,
        worker: WorkerConfig,
        store: WorkerStore,
        inference: Optional[Inference] = None,
        registry: Optional[ToolRegistry] = None,
    ):
        self.worker = worker
        self.store = store
        self.llm = inference or NullInference()
        base = registry or build_registry()
        self.registry = base.subset(worker.tools) if worker.tools else base
        self.approvals = ApprovalManager(store, worker=self.worker)
        self._active_ctx: Optional[ToolContext] = None  # ctx of the in-flight run, for cancel()

        # §20 connector architecture: default-deny registry. A worker must
        # explicitly declare connectors; nothing external is reachable otherwise.
        self.connectors = ConnectorManager(
            specs=worker.connectors or None,
            secret_resolver=self._resolve_secret,
        )

    def _resolve_secret(self, ref: str) -> str:
        """Resolve a secret ref from the §8 store. Fail-closed: any failure
        (store unavailable, crypto missing, secret absent) surfaces as an
        exception the ConnectorManager turns into a refusal."""
        from .secrets import SecretStore

        key_path = os.path.join(self.store.root, "secrets.key")
        try:
            ss = SecretStore(self.store, key_path=key_path)
        except Exception as exc:
            raise ConnectorError(f"secret store unavailable: {exc}") from exc
        return ss.get(ref)

    # -- context -----------------------------------------------------------
    def _tool_ctx(self, run) -> ToolContext:
        run_id = run.id if hasattr(run, "id") else run.get("id", "")
        return ToolContext(
            worker=self.worker.name,
            run_id=run_id,
            workspace=self.worker.workspace,
            fs_roots=self.worker.resolved_fs_roots(),
            artifacts_dir=self.worker.artifacts_dir(),
            shell_allow=list(self.worker.shell_allow),
            env_allow=list(self.worker.env_allow),
            timeout=self.worker.timeout,
            max_output=self.worker.max_output,
            max_python_runtime=self.worker.max_python_runtime,
            max_shell_runtime=self.worker.max_shell_runtime,
            browser_allow=list(self.worker.browser_allow),
            browser_timeout=self.worker.browser_timeout,
            browser_downloads=self.worker.browser_downloads,
            browser_uploads=self.worker.browser_uploads,
            browser_credential_refs=list(self.worker.browser_credential_refs),
            browser_private_session=self.worker.browser_private_session,
            secret_resolver=self._resolve_secret,
            message_allow=list(self.worker.message_allow),
            message_rate_limit=self.worker.message_rate_limit,
            sandbox=self.worker.sandbox,
            egress_allow=list(self.worker.egress_allow),
            dlp_rules=list(self.worker.dlp_rules),
        )

    def _tool_catalog(self) -> str:
        lines = []
        for spec in self.registry.specs():
            props = spec["input_schema"].get("properties", {})
            args = ", ".join(
                f"{k}:{v.get('type','any')}" for k, v in props.items()
            )
            lines.append(
                f"- {spec['name']}({args}) [{spec['risk_level']}"
                f"{', approval required' if spec['requires_approval'] else ''}] "
                f"{spec['description']}"
            )
        return "\n".join(lines)

    # -- §20 connectors -----------------------------------------------------

    def connector_action(
        self, kind: str, action: str, target: str, args: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute a governed connector action.

        This is the ONLY path through which a worker reaches an external system
        via a connector. It is fail-closed: the action is refused unless the
        connector is enabled AND the target passes its allow-list AND (if the
        connector needs credentials) the secret refs resolve. No credential value
        is returned in the result — only confirmations and transport handles.
        """
        args = args or {}
        ok, reason, conn = self.connectors.authorize(kind, action, target)
        if not ok:
            return {
                "ok": False,
                "refused": True,
                "reason": reason,
                "kind": kind,
                "target": target,
            }
        try:
            creds = self.connectors.resolve_credentials(kind)
        except ConnectorError as exc:
            return {
                "ok": False,
                "refused": True,
                "reason": f"credential resolution failed: {exc}",
                "kind": kind,
                "target": target,
            }
        if conn is None:
            return {
                "ok": False,
                "refused": True,
                "reason": f"connector {kind!r} became unavailable",
                "kind": kind,
                "target": target,
            }
        plan = conn.execute(action, target, args, creds)
        # The plan is a transport descriptor, not an execution result: the engine
        # returns what WOULD run and what credentials names are in play, never the
        # values. Actual transmission is performed by the delegated tool inside
        # the worker's sandbox, where the resolver-injected env var lives.
        return {
            "ok": True,
            "kind": kind,
            "action": action,
            "target": target,
            "plan": {k: v for k, v in plan.items() if k != "args"} | {"args_keys": list(plan.get("args", {}))},
            "credentials_used": sorted(creds.keys()),
        }

    # -- lifecycle ---------------------------------------------------------
    def run(
        self,
        request: str,
        *,
        procedure: str = "",
        trigger: str = "manual",
        inputs: Optional[Dict[str, Any]] = None,
        resume_run_id: str = "",
        on_event=None,
    ) -> RunResult:
        if resume_run_id:
            return self._resume(resume_run_id, on_event=on_event)
        # §26 — disabled workers are refused at the boundary (fail closed).
        if getattr(self.worker, "disabled", False):
            raise RuntimeError(
                f"worker {self.worker.name!r} is disabled; enable it before running "
                f"(sworker worker enable {self.worker.name})"
            )

        task = Task(
            worker=self.worker.name,
            request=request,
            trigger=trigger,
            procedure=procedure,
            inputs=dict(inputs or {}),
        )
        self.store.put("tasks", task, event="task.created")
        run = Run(worker=self.worker.name, task_id=task.id, trigger=trigger, procedure=procedure)
        self.store.put("runs", run, event="run.started")
        self._emit(on_event, "run.started", {"run_id": run.id, "request": request})
        # lifecycle: PENDING -> PLANNING
        from . import statemachine as sm

        sm.transition(run, RunStatus.PLANNING, store=self.store, actor="engine", reason="planning")

        ledger = EvidenceLedger(self.store, run.id)
        guard = DecompositionGuard()
        perms = PermissionEngine(self.worker, guard)
        ctx = self._tool_ctx(run)
        self._active_ctx = ctx

        # §61 — graceful degradation ledger for this run.
        deg = DegradationLedger(self.store, run_id=run.id)
        if not self.llm.available():
            deg.record(
                MODEL_FALLBACK,
                "no reachable language model; using deterministic fallback planner",
                severity="warn",
                mitigation="start a local model at SWORKER_LLM_URL / --llm-url",
            )

        # §62 — safe mode is read once per run and applied at execution time.
        # When enabled it fails closed: any action above the permitted risk is
        # blocked and recorded as a critical degradation (see _execute).
        safe = SafeMode(self.store)

        # §63 — incident response: while an incident is open the platform is
        # frozen, so we must not launch *new* work into it. We block here, before
        # any planning or execution, and record a critical degradation so the run
        # is reported BLOCKED rather than silently dropped.
        from .incident import IncidentLedger
        if IncidentLedger(self.store).active():
            deg.record(
                INCIDENT_ACTIVE,
                "an incident is open; new runs are refused until it is closed",
                severity=CRITICAL,
                mitigation="sworker incident close  (then sworker safemode off to stand the platform back down)",
            )
            run.summary = "blocked: an incident is open"
            run.error = "incident_active"
            run.finished = now()
            run.degradations = deg.summary()
            sm.transition(run, RunStatus.BLOCKED, store=self.store, actor="engine", reason="incident active")
            self._emit(on_event, "run.blocked", {"run_id": run.id, "reason": "incident_active"})
            return RunResult(
                run=run,
                status=RunStatus.BLOCKED,
                summary=run.summary,
                artifacts=[],
                claims=[],
                pending_approvals=[],
            )

        # --- INTENT + PLAN ---
        intent, steps = self._plan(request, procedure, inputs or {})
        run.intent = intent
        plan = Plan(run_id=run.id, intent=intent, rationale="planner" if self.llm.available() else "deterministic fallback")
        self.store.put("plans", plan, event="plan.created")
        self._emit(on_event, "plan.created", {"intent": intent, "steps": len(steps)})

        step_records: List[Step] = []
        for i, s in enumerate(steps[:MAX_STEPS]):
            rec = Step(
                run_id=run.id,
                plan_id=plan.id,
                index=i,
                description=str(s.get("description") or s.get("tool") or f"step {i}"),
                tool=str(s.get("tool") or ""),
                args=dict(s.get("args") or {}),
            )
            self.store.put("steps", rec, event="step.planned")
            step_records.append(rec)
        plan.step_ids = [s.id for s in step_records]
        self.store.put("plans", plan, event="plan.finalized")

        return self._execute(run, plan, step_records, ledger, perms, ctx, on_event, deg, computed=[], safe=safe)

    # -- planning ----------------------------------------------------------
    def _plan(
        self, request: str, procedure: str, inputs: Dict[str, Any]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        if procedure:
            from .procedures import load_procedure, procedure_steps

            proc = load_procedure(self.worker, procedure)
            if proc:
                return (
                    proc.get("intent") or f"execute procedure {procedure}",
                    procedure_steps(proc, inputs),
                )

        prompt = (
            f"Worker: {self.worker.name} ({self.worker.role})\n"
            f"Instructions: {self.worker.instructions}\n\n"
            f"Available tools:\n{self._tool_catalog()}\n\n"
            f"Company data lives under company/ in the workspace.\n"
            f"Request: {request}\n\n"
            "Emit the JSON plan now."
        )
        data = self.llm.complete_json(prompt, system=PLANNER_SYSTEM)
        if isinstance(data, dict) and isinstance(data.get("steps"), list) and data["steps"]:
            steps = []
            for s in data["steps"]:
                if not isinstance(s, dict):
                    continue
                tool = s.get("tool")
                if tool and not self.registry.has(str(tool)):
                    # keep the step as a reasoning step rather than inventing a tool
                    s = {**s, "tool": "", "args": {}, "description": f"[unavailable tool {tool}] {s.get('description','')}"}
                steps.append(s)
            if steps:
                return str(data.get("intent") or request), steps
        return self._fallback_plan(request)

    def _fallback_plan(self, request: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Deterministic plan used when no model is reachable.

        This is NOT a pretend-agent: it does real retrieval over real files and
        says plainly in the artifact that it ran without a language model. When
        the question is quantitative and the company data holds CSVs, it finds a
        numeric column and records a deterministic sum so the run still answers.
        """
        steps: List[Dict[str, Any]] = [
            {
                "description": "Search compiled company knowledge for the request",
                "tool": "knowledge.search",
                "args": {"query": request, "limit": 8},
            },
            {
                "description": "List available company data files",
                "tool": "fs.list",
                "args": {"path": "company", "pattern": "*.csv"},
            },
        ]
        # If the question asks for a total/sum and we can read a CSV, compute it.
        quant = any(w in request.lower() for w in ("total", "sum", "how much", "revenue", "sales"))
        if quant:
            best = self._best_total_target(self.worker.workspace, request)
            if best:
                fn, col, where, gcol = best
                args: Dict[str, Any] = {"path": f"company/{fn}", "agg": "sum", "value_column": col}
                if where:
                    args["where"] = where
                if gcol:
                    args["group_by"] = gcol
                steps.append(
                    {
                        "description": f"Compute total {col} from {fn}"
                        + (f" where {where}" if where else "")
                        + (f" by {gcol}" if gcol else ""),
                        "tool": "data.query",
                        "args": args,
                    }
                )
                # also write a short evidence-backed report artifact
                steps.append(
                    {
                        "description": f"Write a report of the {col} result to artifacts",
                        "tool": "fs.write",
                        "args": {
                            "path": f"artifacts/q2_{col}_report.md",
                            "content": (
                                f"# {col.title()} report (auto, no language model)\n\n"
                                f"Request: {request}\n\n"
                                f"This report was produced by the deterministic fallback (no language "
                                f"model was reachable) by directly querying `{fn}`.\n\n"
                                f"- Source: company/{fn}\n"
                                f"- Filter: {where or 'none'}\n"
                                f"- Group: {gcol or 'total'}\n\n"
                                f"See the run's evidence log for the exact computed figure and the "
                                f"file checksum that proves it.\n"
                            ),
                        },
                    }
                )
        return (f"(no language model available) retrieve company context for: {request}", steps)

    @staticmethod
    def _best_total_target(workspace: str, request: str):
        """Pick the CSV/column most likely to answer a quantitative question, with
        no model. Returns (filename, column, where-filter-or-None, group-by-or-None)."""
        import os as _os

        company = _os.path.join(workspace, "company")
        # strip punctuation so "Q1?" / "Q2." still match the quarter token
        low = _os.path.normcase(request.lower())
        import re as _re

        tokens = _re.sub(r"[^a-z0-9]+", " ", low).split()
        quarter = None
        for q in ("q1", "q2", "q3", "q4"):
            if q in tokens:
                quarter = q
                break
        want_group = any(k in low for k in ("channel", "region", "by ", "per ", "breakdown", "store"))
        candidates: List[Tuple[str, str, Any, Any, int]] = []
        for fn in sorted(_os.listdir(company)):
            if not fn.endswith(".csv"):
                continue
            path = _os.path.join(company, fn)
            try:
                from .tools.data import _num, read_csv

                rows = read_csv(path)
            except Exception:
                continue
            if not rows:
                continue
            headers = list(rows[0].keys())
            numeric = [c for c in headers if all(_num(r.get(c)) is not None for r in rows[:50])]
            if not numeric:
                continue
            chosen_col = None
            for c in numeric:
                if c.lower() in low:
                    chosen_col = c
                    break
            if chosen_col is None and any(tok in fn.lower() for tok in low.split() if len(tok) > 3):
                chosen_col = numeric[-1]
            if chosen_col is None:
                # remember as a last resort, but keep scanning for a better match
                if not candidates:
                    candidates.append((fn, numeric[-1], None, None, len(numeric)))
                continue
            where = None
            if quarter and "quarter" in [h.lower() for h in headers]:
                where = {"quarter": quarter.upper()}
            gcol = None
            if want_group:
                for cand in ("region", "channel", "store", "product", "category"):
                    if cand in [h.lower() for h in headers]:
                        gcol = cand
                        break
            score = 0
            low_fn = fn.lower()
            for tok in low.split():
                if len(tok) > 3 and tok in low_fn:
                    score += 10
            for c in numeric:
                if c.lower() in low:
                    score += 5
            score += len(rows)  # prefer the data-richer file
            candidates.append((fn, chosen_col, where, gcol, score))
        if not candidates:
            return None
        # best score first; never report from a stray sample file (e.g.
        # example.csv) when a request-specific, data-heavier CSV exists.
        candidates.sort(key=lambda c: c[4], reverse=True)
        fn, col, where, gcol, _ = candidates[0]
        return fn, col, where, gcol

    @staticmethod
    def _guess_numeric_column(path: str, request: str = "") -> Optional[str]:
        try:
            from .tools.data import _num, read_csv

            rows = read_csv(path)
            if not rows:
                return None
            numeric_cols = [
                c for c, v in rows[0].items() if all(_num(r.get(c)) is not None for r in rows[:50])
            ]
            if not numeric_cols:
                return None
            # Prefer a column whose name appears in the request, else the last
            # (id-like columns like 'x' usually come first; totals last).
            low = request.lower()
            for c in numeric_cols:
                if c.lower() in low:
                    return c
            return numeric_cols[-1]
        except Exception:
            return None

    # -- execution ---------------------------------------------------------
    def _execute(
        self,
        run: Run,
        plan: Plan,
        steps: List[Step],
        ledger: EvidenceLedger,
        perms: PermissionEngine,
        ctx: ToolContext,
        on_event,
        deg: Optional[DegradationLedger] = None,
        computed: Optional[List[Dict[str, Any]]] = None,
        safe: Optional["SafeMode"] = None,
    ) -> RunResult:
        if deg is None:
            deg = DegradationLedger(self.store, run_id=run.id)
        failures = 0
        executed = 0
        blocked = False
        awaiting = False
        if computed is None:
            computed = []

        # --- resource accounting (spec §10) ---------------------------------
        cfg = self.worker
        budget = {
            "actions": cfg.max_actions,
            "tool_calls": cfg.max_tool_calls,
            "network": cfg.max_network_requests,
        }
        started_at = time.time()
        max_runtime = cfg.max_runtime
        resource_error = {"msg": ""}

        def _over_runtime() -> bool:
            return (time.time() - started_at) > max_runtime

        def _exhausted() -> str:
            if budget["actions"] <= 0:
                return "max_actions exceeded"
            if budget["tool_calls"] <= 0:
                return "max_tool_calls exceeded"
            if budget["network"] <= 0:
                return "max_network_requests exceeded"
            if _over_runtime():
                return "max_runtime exceeded"
            return ""

        # watchdog: if the wall-clock budget is blown, kill the whole run.
        def _watchdog() -> None:
            while time.time() - started_at <= max_runtime:
                time.sleep(0.25)
            if self._active_ctx is not None and self._active_ctx.run_id == run.id:
                self._kill_active_ctx()

        threading.Thread(target=_watchdog, name="sworker-watchdog", daemon=True).start()

        from . import statemachine as sm

        sm.transition(run, RunStatus.EXECUTING, store=self.store, actor="engine", reason="executing")

        # §61 — if this run leans on compiled knowledge but Atlas is unavailable,
        # record the degradation (the knowledge tools still function, but on a
        # degraded plaintext backend). Fail-closed: absence is never "success".
        if any(t in self.registry.names() for t in ("knowledge_search", "knowledge_compile")):
            try:
                from . import knowledge as K

                if not K.atlas_status()["available"]:
                    deg.record(
                        KNOWLEDGE_UNCOMPILED,
                        "Hermes Atlas unavailable; knowledge served via uncompiled plaintext grep",
                        severity="warn",
                        mitigation="install hermes-atlas / set SWORKER_ATLAS_HOME",
                    )
            except Exception:
                # probing Atlas must never break a run; a probe failure is itself
                # a degradation worth recording, but we keep it quiet.
                pass

        for step in steps:
            # resource guard: bail out the moment any budget is blown (spec §10).
            exhausted = _exhausted()
            if exhausted:
                resource_error["msg"] = exhausted
                run.error = exhausted
                break

            if not step.tool:
                step.status = StepStatus.SKIPPED
                step.note = "reasoning step; no tool invocation"
                self.store.put("steps", step, event="step.skipped")
                continue

            try:
                tool = self.registry.get(step.tool)
            except ToolError as exc:
                step.status = StepStatus.FAILED
                step.note = str(exc)
                self.store.put("steps", step, event="step.failed")
                failures += 1
                continue

            try:
                args = tool.validate(step.args)
            except ToolError as exc:
                step.status = StepStatus.FAILED
                step.note = f"invalid arguments: {exc}"
                self.store.put("steps", step, event="step.failed")
                failures += 1
                self._emit(on_event, "step.failed", {"step": step.description, "error": str(exc)})
                continue

            decision = perms.evaluate(tool, args)
            action = Action(
                run_id=run.id,
                step_id=step.id,
                tool=tool.name,
                args=args,
                risk=decision.risk,
                summary=tool.summarize(args),
                rationale=decision.reason,
            )
            self.store.put("actions", action, event="action.proposed")

            # §62 — safe mode fails closed. If the current safe-mode level
            # forbids this action's risk, block it and record a CRITICAL
            # degradation so the run is never reported as a clean SUCCESS. We
            # block *before* approval/deny handling: safe mode overrides both —
            # an operator in incident response would rather re-trigger a blocked
            # action than have it slip through an auto-approved path.
            if safe is not None and safe.is_blocked(decision.risk):
                if not any(e.category == SAFE_MODE_BLOCK for e in deg.entries()):
                    deg.record(
                        SAFE_MODE_BLOCK,
                        safe.reason(decision.risk),
                        severity=CRITICAL,
                        mitigation="disable safe mode or raise its level (sworker safemode off / locked)",
                    )
                action.status = ActionStatus.DENIED
                self.store.put("actions", action, event="action.denied")
                step.status = StepStatus.BLOCKED
                step.note = safe.reason(decision.risk)
                self.store.put("steps", step, event="step.blocked")
                blocked = True
                self._emit(on_event, "action.denied", {"summary": action.summary, "reason": safe.reason(decision.risk)})
                continue
            budget["actions"] -= 1
            budget["tool_calls"] -= 1
            if "network" in tool.categories:
                budget["network"] -= 1

            if decision.denied:
                action.status = ActionStatus.DENIED
                self.store.put("actions", action, event="action.denied")
                step.status = StepStatus.BLOCKED
                step.note = decision.reason
                self.store.put("steps", step, event="step.blocked")
                blocked = True
                self._emit(on_event, "action.denied", {"summary": action.summary, "reason": decision.reason})
                continue

            if decision.needs_approval:
                evs = ledger.all_evidence()[-5:]
                appr = self.approvals.request(
                    action,
                    summary=action.summary,
                    reason=decision.reason,
                    evidence_ids=[e["id"] for e in evs],
                )
                perms.guard.record_pending(decision.risk)
                step.status = StepStatus.AWAITING_APPROVAL
                step.note = f"approval {appr.id} requested"
                self.store.put("steps", step, event="step.awaiting_approval")
                sm_transition = __import__("sworker.statemachine", fromlist=["transition"]).transition
                sm_transition(run, RunStatus.AWAITING_APPROVAL, store=self.store, actor="engine", reason=f"awaiting approval {appr.id}")
                awaiting = True
                self._emit(
                    on_event,
                    "approval.requested",
                    {"id": appr.id, "summary": action.summary, "risk": decision.risk.value},
                )
                continue

            ok = self._execute_action(run, step, action, tool, args, ctx, ledger, on_event, computed)
            executed += 1
            if not ok:
                failures += 1

        # verification of the run's claims — including checks auto-derived from
        # the figures the engine actually computed (so every stated number is
        # independently re-derived from source data before the run is final).
        run.verifications = self._derive_verifications(computed)
        self._verify_run(run, ledger, ctx)

        from . import statemachine as sm

        if not awaiting:
            # If we were awaiting approval (a prior step), we resume into
            # EXECUTING; otherwise move EXECUTING -> VERIFYING -> terminal.
            if run.status == RunStatus.AWAITING_APPROVAL:
                sm.transition(run, RunStatus.EXECUTING, store=self.store, actor="engine", reason="resumed after approval")
            sm.transition(run, RunStatus.VERIFYING, store=self.store, actor="engine", reason="verifying")

        # If the deterministic fallback wrote a report artifact, fold the
        # derived figure(s) into it so the artifact proves its own numbers.
        if computed:
            arts = self.store.find("artifacts", run_id=run.id, order="created")
            self._backfill_artifact_figures(run, arts, computed, ctx)

        return self._finalize(run, ledger, deg, failures, executed, blocked, awaiting, on_event, computed, resource_error["msg"])

    def _execute_action(
        self, run, step, action, tool, args, ctx, ledger: EvidenceLedger, on_event, computed=None
    ) -> bool:
        if computed is None:
            computed = []
        action.status = ActionStatus.EXECUTING
        self.store.put("actions", action, event="action.executing")
        step.status = StepStatus.RUNNING
        self.store.put("steps", step, event="step.running")
        t0 = time.time()

        attempts = 0
        result = None
        while attempts < 2:
            attempts += 1
            try:
                result = tool.run(ctx, args)
            except ToolError as exc:
                result = type("R", (), {})()  # placeholder replaced below
                from .tools.base import ToolResult

                result = ToolResult(False, error=f"invalid invocation: {exc}")
                break
            except Exception as exc:
                from .tools.base import ToolResult

                result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
            if result.ok:
                break
            if attempts < 2:
                self._emit(on_event, "step.retry", {"step": step.description, "error": result.error})

        assert result is not None
        # §44 — scan the ingested content for prompt-injection attempts before
        # it is trusted. The verdict is recorded on the observation (auditably)
        # but never used to escalate the run's risk or spawn new tool calls.
        inj = _scan_injection({"output": result.output, "error": result.error, **(result.data or {})})
        obs = Observation(
            run_id=run.id,
            action_id=action.id,
            ok=result.ok,
            output=result.output[: self.worker.max_output],
            error=result.error,
            data=result.data,
            truncated=result.truncated,
            duration_ms=int((time.time() - t0) * 1000),
            injection=inj.rule or "",
        )
        self.store.put("observations", obs, event="observation.recorded")
        action.observation_id = obs.id
        action.status = ActionStatus.EXECUTED if result.ok else ActionStatus.FAILED
        self.store.put("actions", action, event="action.completed")

        evs = ledger.from_observation(obs, tool.name, result.evidence)
        run_claims = self.store.find("claims", run_id=run.id)
        for path in result.artifacts:
            art = Artifact(
                run_id=run.id,
                path=path,
                kind=os.path.splitext(path)[1].lstrip(".") or "file",
                bytes=os.path.getsize(path) if os.path.exists(path) else 0,
                description=step.description,
            )
            # §16 claim exposure: record which claims this artifact surfaces so a
            # reader (and the fail-closed finalizer) can trace every stated claim
            # back to its provenance. A claim is "exposed" if its text appears in
            # the artifact body.
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as _afh:
                    _body = _afh.read()
            except OSError:
                _body = ""
            exposed = [
                c["id"] for c in run_claims
                if c.get("text") and c["text"].strip() and c["text"].strip() in _body
            ]
            if exposed:
                art.claim_ids = exposed
            self.store.put("artifacts", art, event="artifact.created")

        step.status = StepStatus.DONE if result.ok else StepStatus.FAILED
        step.note = "" if result.ok else result.error
        step.observation_id = obs.id
        self.store.put("steps", step, event="step.done" if result.ok else "step.failed")
        # Capture computed figures so the run summary can state the derived
        # number (the product's core promise: never state an underived figure).
        if result.ok and result.data:
            d = result.data
            if d.get("groups") is not None and tool.name == "data.query":
                label = f"{d.get('agg','sum')}({d.get('value_column','')})"
                for g in d["groups"]:
                    computed.append({
                        "tool": tool.name,
                        "data": d,
                        "args": args,
                        "summary": f"{label} {g['key']}={g['value']} ({g['rows']} rows)",
                    })
            elif "value" in d and tool.name == "data.query":
                label = f"{d.get('agg','sum')}({d.get('value_column','')})"
                computed.append({
                    "tool": tool.name,
                    "data": d,
                    "args": args,
                    "summary": f"{label}={d['value']} over {d.get('matched_rows',0)}/{d.get('total_rows',0)} rows",
                })
        self._emit(
            on_event,
            "step.done" if result.ok else "step.failed",
            {
                "step": step.description,
                "tool": tool.name,
                "ok": result.ok,
                "error": result.error,
                "evidence": len(evs),
            },
        )
        return result.ok

    # -- verification ------------------------------------------------------
    def _derive_verifications(self, computed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build recompute_sum checks from the figures the engine derived.

        Every number the run states comes from a data.query result. We turn each
        derived (path, column, where, value) into a deterministic check that
        re-sums the same source rows and compares to the derived value — so a
        run can be independently re-verified later (e.g. via the web UI) without
        trusting the engine's own arithmetic.
        """
        specs: List[Dict[str, Any]] = []
        for c in computed:
            d = c.get("data")
            if not d or d.get("agg", "sum") == "count":
                continue
            p = d.get("path", "")
            vcol = d.get("value_column")
            if not p or not vcol:
                continue
            rel = os.path.relpath(os.path.realpath(p), os.path.realpath(self.worker.workspace))
            # Use the real filter the tool actually applied (args), falling back
            # to whatever the data dict carried.
            a = c.get("args") or {}
            base_where = dict(a.get("where") or d.get("where") or {})
            gcol = a.get("group_by") or d.get("group_by")
            tol = 0.01
            if d.get("groups") is not None:
                for g in d["groups"]:
                    where = dict(base_where)
                    if gcol:
                        where[gcol] = g["key"]
                    specs.append({
                        "check": "recompute_sum",
                        "path": rel,
                        "value_column": vcol,
                        "where": where,
                        "expect": g["value"],
                        "tolerance": tol,
                    })
            else:
                specs.append({
                    "check": "recompute_sum",
                    "path": rel,
                    "value_column": vcol,
                    "where": base_where,
                    "expect": d.get("value"),
                    "tolerance": tol,
                })
        return specs

    def _verify_run(self, run: Run, ledger: EvidenceLedger, ctx: ToolContext) -> None:
        """Run any verification specs the worker/procedure declared."""
        for spec in getattr(run, "verifications", None) or []:
            self.record_verification(run, spec, ctx, claim_id="")

    def record_verification(
        self, run, spec: Dict[str, Any], ctx: ToolContext, claim_id: str = ""
    ) -> Verification:
        run_id = run.id if hasattr(run, "id") else run.get("id", "")
        res = verify_mod.run_check(spec, ctx.workspace)
        ver = Verification(
            run_id=run_id,
            claim_id=claim_id,
            check=res.check,
            outcome=res.status,
            detail=res.detail,
            expected="" if res.expected is None else str(res.expected),
            actual="" if res.actual is None else str(res.actual),
        )
        self.store.put("verifications", ver, event="verification.recorded")
        if claim_id:
            claim = self.store.get("claims", claim_id)
            if claim:
                claim["verification_ids"] = list(claim.get("verification_ids") or []) + [ver.id]
                if res.status is VerificationOutcome.PASS:
                    claim["provenance"] = Provenance.VERIFIED.value
                    claim["confidence"] = "HIGH"
                elif res.status is VerificationOutcome.FAIL:
                    claim["refuted"] = True
                    claim["confidence"] = "LOW"
                self.store.put("claims", claim, event="claim.verified")
        return ver

    def _backfill_artifact_figures(self, run, arts, computed, ctx) -> None:
        """Append the derived figures to the fallback report artifact.

        The deterministic fallback writes its report before any number is known
        (plan time). After execution we know the computed value, so we rewrite
        the artifact to state it — keeping the product's promise that every
        stated number is derived, never invented.
        """
        if not arts or not computed:
            return
        figure_line = "Derived figures (computed from source CSV):\n" + "\n".join(
            f"- {c['summary']}" for c in computed if c.get("summary")
        )
        for art in arts:
            p = art["path"]
            try:
                rp = ctx.resolve(p)
            except Exception:
                continue
            if not os.path.isfile(rp):
                continue
            try:
                with open(rp, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "Derived figures" in text:
                continue
            text = text.rstrip() + "\n\n" + figure_line + "\n"
            with open(rp, "w", encoding="utf-8") as fh:
                fh.write(text)
            art["bytes"] = os.path.getsize(rp)
            self.store.put("artifacts", art, event="artifact.updated")

    # -- finalization ------------------------------------------------------
    def _finalize(
        self, run: Run, ledger: EvidenceLedger, deg: Optional[DegradationLedger] = None, failures=0, executed=0, blocked=False, awaiting=False, on_event=None,
        computed: Optional[List[Dict[str, Any]]] = None, resource_exhausted: str = "",
    ) -> RunResult:
        if deg is None:
            deg = DegradationLedger(self.store, run_id=getattr(run, "id", ""))
        if computed is None:
            computed = []
        evidence = ledger.all_evidence()
        claims = ledger.all_claims()
        arts = self.store.find("artifacts", run_id=run.id, order="created")
        pending = self.approvals.pending(run.id)
        vers = self.store.find("verifications", run_id=run.id)
        failed_vers = [v for v in vers if v["outcome"] == VerificationOutcome.FAIL.value]
        unverifiable_vers = [
            v for v in vers if v["outcome"] == VerificationOutcome.UNVERIFIABLE.value
        ]
        declared_verifications = bool(getattr(run, "verifications", None)) or bool(vers)

        if resource_exhausted:
            status = RunStatus.BLOCKED
            run.error = resource_exhausted
        elif awaiting or pending:
            status = RunStatus.AWAITING_APPROVAL
        elif blocked:
            status = RunStatus.BLOCKED
        elif failed_vers:
            status = RunStatus.PARTIAL_SUCCESS
        elif executed == 0:
            status = RunStatus.FAILED if failures else RunStatus.INSUFFICIENT_EVIDENCE
        elif failures and failures >= executed:
            status = RunStatus.FAILED
        elif failures:
            status = RunStatus.PARTIAL_SUCCESS
        elif not evidence:
            status = RunStatus.INSUFFICIENT_EVIDENCE
        else:
            status = RunStatus.SUCCESS

        # Fail-closed verification hardening (§7): if this run declared or
        # produced verification checks, an UNVERIFIABLE result (unknown check,
        # error during recompute, missing expected value) must NOT be reported
        # as full SUCCESS — it means a number could not actually be proven, so
        # the run is at most PARTIAL_SUCCESS. A clean SUCCESS requires every
        # declared check to PASS.
        if status is RunStatus.SUCCESS and declared_verifications and unverifiable_vers:
            status = RunStatus.PARTIAL_SUCCESS
            run.error = run.error or (
                f"{len(unverifiable_vers)} verification check(s) could not be proven"
            )

        # Fail-closed claim exposure (§16): an artifact that surfaces claims to a
        # reader must not be reported full SUCCESS if those exposed claims carry
        # no provenance at all (no evidence link, no verification). A claim
        # exported with nothing backing it is unprovable — exactly the silent
        # SUCCESS this engine forbids. Refuted/hypothesized-but-unverified claims
        # keep the run at PARTIAL_SUCCESS; the presence of ANY backed claim clears
        # the bar (the artifact then exposes both sourced and unsourced claims,
        # which a reader can distinguish via claim.provenance).
        if status is RunStatus.SUCCESS:
            exposed_ids = {cid for a in arts for cid in (a.get("claim_ids") or [])}
            if exposed_ids:
                claims_by_id = {c["id"]: c for c in claims}
                unbacked = [
                    cid for cid in exposed_ids
                    if cid in claims_by_id
                    and not (claims_by_id[cid].get("evidence_ids") or claims_by_id[cid].get("verification_ids"))
                ]
                if unbacked:
                    status = RunStatus.PARTIAL_SUCCESS
                    run.error = run.error or (
                        f"{len(unbacked)} surfaced claim(s) have no provenance (evidence/verification)"
                    )

        # §61 — surface recorded degradations on the run, and fail-closed: a
        # *critical* degradation (a safety/correctness capability that could not
        # run) must not be reported as full SUCCESS. Degradations are already
        # persisted + audited where they were recorded; here we make them
        # visible on the run result and apply the downgrade.
        run.degradations = deg.summary()

        # §61 — fail-closed downgrade: a critical degradation that occurred wins
        # over a SUCCESS verdict. We re-check after the claim-exposure block so
        # neither can silently mask the other.
        if status is RunStatus.SUCCESS and deg.any_critical():
            status = RunStatus.PARTIAL_SUCCESS
            run.error = run.error or "run completed with a critical capability degradation"


        # Every run passes through VERIFYING on the way to a terminal state;
        # bring it there via the legal path, then to the final status.
        from . import statemachine as sm
        from .statemachine import is_terminal

        if status != run.status and not is_terminal(run.status):
            if run.status == RunStatus.AWAITING_APPROVAL:
                sm.transition(run, RunStatus.EXECUTING, store=self.store, actor="engine", reason="resumed after approval")
            if run.status != RunStatus.VERIFYING:
                sm.transition(run, RunStatus.VERIFYING, store=self.store, actor="engine", reason="verifying")
            sm.transition(run, status, store=self.store, actor="engine", reason="final status")
        run.finished = now()
        run.evidence_count = len(evidence)
        run.claim_count = len(claims)
        run.artifact_ids = [a["id"] for a in arts]
        run.approval_count = len(self.approvals.for_run(run.id))
        run.summary = self._summary_line(status, executed, failures, evidence, arts, pending, computed)
        self.store.put("runs", run, event="run.finished")
        self._emit(on_event, "run.finished", {"status": status.value, "summary": run.summary})

        return RunResult(
            run=run,
            status=status,
            summary=run.summary,
            artifacts=[Artifact(**{k: v for k, v in a.items() if k in Artifact.__dataclass_fields__}) for a in arts],
            claims=[],
            pending_approvals=pending,
        )

    @staticmethod
    def _summary_line(status, executed, failures, evidence, arts, pending, computed=None) -> str:
        bits = [
            f"{status.value}",
            f"{executed} action(s) executed",
            f"{failures} failed",
            f"{len(evidence)} evidence item(s)",
            f"{len(arts)} artifact(s)",
        ]
        if pending:
            bits.append(f"{len(pending)} awaiting approval")
        if computed:
            # state the derived figure(s) so the summary is self-proving
            bits.append("computed: " + "; ".join(c.get("summary", "") for c in computed if c.get("summary")))
        return "; ".join(bits)

    # -- resume after approval --------------------------------------------
    def _resume(self, run_id: str, on_event=None) -> RunResult:
        rec = self.store.get("runs", run_id)
        if not rec:
            raise KeyError(f"no run {run_id!r}")
        run = Run(**{k: v for k, v in rec.items() if k in Run.__dataclass_fields__})
        # A run that is already in a terminal state has nothing to resume;
        # re-finalizing it would corrupt the audit trail. Refuse cleanly.
        from .statemachine import is_terminal, RunStatus as _RS

        if is_terminal(RunStatus(run.status)):
            return RunResult(
                run=run,
                status=RunStatus(run.status),
                summary=run.summary or f"run already {run.status}",
                artifacts=[],
                claims=[],
                pending_approvals=[],
            )
        ledger = EvidenceLedger(self.store, run.id)
        guard = DecompositionGuard()
        deg = DegradationLedger(self.store, run_id=run.id)
        for appr in self.approvals.for_run(run.id):
            if appr["state"] == "REJECTED":
                guard.record_rejection(appr["risk"])
        perms = PermissionEngine(self.worker, guard)
        ctx = self._tool_ctx(run)

        executed = failures = 0
        blocked = False
        for appr in self.approvals.for_run(run.id):
            act_rec = self.store.get("actions", appr["action_id"])
            if not act_rec:
                continue
            step_rec = self.store.get("steps", act_rec["step_id"])
            if appr["state"] == "APPROVED" and act_rec["status"] == ActionStatus.APPROVED.value:
                action = Action(**{k: v for k, v in act_rec.items() if k in Action.__dataclass_fields__})
                step = Step(**{k: v for k, v in step_rec.items() if k in Step.__dataclass_fields__})
                tool = self.registry.get(action.tool)
                ok = self._execute_action(
                    run, step, action, tool, action.args, ctx, ledger, on_event
                )
                executed += 1
                failures += 0 if ok else 1
            elif appr["state"] == "REJECTED":
                if step_rec:
                    step_rec["status"] = StepStatus.BLOCKED.value
                    step_rec["note"] = f"human rejected approval {appr['id']}"
                    self.store.put("steps", step_rec, event="step.blocked")
                blocked = True

        return self._finalize(run, ledger, deg, failures, executed, blocked, False, on_event)

    # -- cancellation (spec §11) -------------------------------------------
    def cancel(self, run_id: str, by: str = "user", reason: str = "") -> Run:
        """Cancel a run. Idempotent: cancelling an already-terminal run is a no-op.

        Propagates to any live child subprocesses (kills their process group),
        marks the current step/action CANCELLED, transitions the run to the
        CANCELLED terminal state, and records who/when/why.
        """
        rec = self.store.get("runs", run_id)
        if not rec:
            raise KeyError(f"no run {run_id!r}")
        run = Run(**{k: v for k, v in rec.items() if k in Run.__dataclass_fields__})
        from .statemachine import is_terminal, RunStatus as _RS

        if is_terminal(_RS(run.status)):
            return run

        # kill any live subprocesses spawned by this run's tools (whole group)
        self._kill_active_ctx(run_id)

        # --- cancel-specific bookkeeping (only runs on cancel, not watchdog) ---
        # mark the in-flight step/action cancelled, if any
        in_flight = self.store.find("steps", run_id=run.id, status=StepStatus.RUNNING.value)
        for s in in_flight:
            s["status"] = StepStatus.BLOCKED.value
            s["note"] = f"cancelled by {by}: {reason}".strip()
            self.store.put("steps", s, event="step.cancelled")
        in_flight_act = self.store.find("actions", run_id=run.id, status=ActionStatus.EXECUTING.value)
        for a in in_flight_act:
            a["status"] = ActionStatus.SKIPPED.value
            self.store.put("actions", a, event="action.cancelled")

        run.error = reason or "cancelled"
        run.finished = now()
        from . import statemachine as sm

        sm.transition(run, _RS.CANCELLED, store=self.store, actor=by, reason=reason or "cancelled")
        # stamp the cancel reason on the persisted record (transition reloads +
        # rewrites the run, so this must happen after it)
        persisted = self.store.get("runs", run.id)
        if persisted is not None:
            persisted["error"] = reason or "cancelled"
            persisted["finished"] = run.finished
            self.store.put("runs", persisted, event="run.cancelled")
        return run

    def _kill_active_ctx(self, run_id: str = "") -> None:
        """Kill the live child process group(s) for the active tool context.

        Used both by `cancel()` and the resource-budget watchdog. Safe to call
        when there is no active context.
        """
        ctx = self._active_ctx
        if ctx is None or (run_id and ctx.run_id != run_id):
            return
        for pid in ctx.running_subprocesses:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self._active_ctx = None

    @staticmethod
    def _emit(cb, event: str, payload: Dict[str, Any]) -> None:
        if cb:
            try:
                cb(event, payload)
            except Exception:
                pass
