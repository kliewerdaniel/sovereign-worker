"""The only writer of the sales tables.

Workers never touch sqlite or the filesystem directly: they call sales tools,
which call this repository. That is what makes the boundary in
``docs/SALES_INTEGRATION.md`` real rather than aspirational.

Rules enforced here (not in the tools, so they cannot be bypassed):
  * a lead is deduplicated on a normalised company identity (``dedupe_key``);
  * a stage change must be legal per ``pipeline.can_move`` and is appended to
    ``pipeline_history`` — history is never rewritten;
  * a qualification is append-only: re-scoring inserts a new ``version``;
  * evidence must carry a non-empty ``source_ref``;
  * an outreach draft can only become ``sent`` after it was ``approved``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import fields as dc_fields
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .models import (
    Activity,
    ClaimTier,
    Company,
    Contact,
    FollowUp,
    ICP,
    Lead,
    Opportunity,
    Outcome,
    OutreachDraft,
    OutreachState,
    PainPoint,
    PipelineStage,
    PipelineTransition,
    Proposal,
    Qualification,
    SALES_CLAIM_TYPES,
    SalesEvidenceRecord,
    SalesRecord,
    Task,
    TaskState,
    now,
)

# Per-class enum-field map. Used so a value coming back out of sqlite is restored
# to its Enum (dataclass annotations are strings under ``from __future__ import
# annotations`` and cannot be inspected at runtime).
_ENUM_FIELDS: Dict[type, Dict[str, Any]] = {
    Lead: {"stage": PipelineStage},
    Opportunity: {"stage": PipelineStage},
    SalesEvidenceRecord: {"tier": ClaimTier},
    Qualification: {"tier": ClaimTier},
    PainPoint: {"tier": ClaimTier},
    OutreachDraft: {"state": OutreachState},
    Task: {"state": TaskState},
    FollowUp: {"state": TaskState},
}
from .pipeline import can_move, days_overdue, stage_of
from .schema import ensure_schema

LEDGER_ENV = "DAILYSALESOS_LEDGER"
DEFAULT_RELATIVE = os.path.join("company", "Experiment_Ledger", "experiments.db")

# Columns that hold a JSON-encoded list in sqlite but a list in the dataclass.
_JSON_LIST_FIELDS = {"evidence_ids"}
_JSON_DICT_FIELDS = {"signals"}


class SalesError(Exception):
    """A domain rule was violated. Surfaced to the caller, never swallowed."""


def default_ledger_path(workspace: str = "") -> str:
    """Where the DailySalesOS ledger lives.

    Resolution order: ``DAILYSALESOS_LEDGER`` env var, then
    ``<workspace>/company/Experiment_Ledger/experiments.db``. Keeping it under
    ``company/`` means the existing worker ``fs_roots: [company]`` convention
    already covers it, so a worker's filesystem boundary governs ledger access.
    """
    env = os.environ.get(LEDGER_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = workspace or os.getcwd()
    return os.path.join(os.path.abspath(base), DEFAULT_RELATIVE)


def normalise_company(name: str, domain: str = "") -> str:
    """Deterministic dedupe key. Domain wins when present; else a slugged name.

    Deliberately boring and reproducible: two runs must derive the same key from
    the same inputs, or deduplication is not verifiable.
    """
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d).strip("/")
    if d:
        return f"domain:{d}"
    n = (name or "").strip().lower()
    n = re.sub(r"\b(inc|llc|l\.l\.c|ltd|co|corp|company|group|team|realty|real estate)\b", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    if not n:
        raise SalesError("cannot derive a dedupe key: company needs a name or domain")
    return f"name:{n}"


def _encode(value: Any, key: str) -> Any:
    if isinstance(value, Enum):
        return value.value
    if key in _JSON_LIST_FIELDS or key in _JSON_DICT_FIELDS:
        return json.dumps(value or ([] if key in _JSON_LIST_FIELDS else {}), sort_keys=True)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _decode_row(cls, row: sqlite3.Row) -> Any:
    names = {f.name for f in dc_fields(cls)}
    data: Dict[str, Any] = {}
    enum_map = _ENUM_FIELDS.get(cls, {})
    for k in row.keys():
        if k not in names:
            continue
        v = row[k]
        if k in _JSON_LIST_FIELDS:
            data[k] = json.loads(v) if v else []
        elif k in _JSON_DICT_FIELDS:
            data[k] = json.loads(v) if v else {}
        elif k in ("is_decision_maker", "active"):
            data[k] = bool(v)
        elif k in enum_map:
            data[k] = enum_map[k](v) if v else v
        else:
            data[k] = v
    return cls(**data)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class SalesRepository:
    """Thin, explicit sqlite repository. No ORM, no third-party dependency."""

    def __init__(self, db_path: str, *, ensure: bool = True):
        self.db_path = os.path.abspath(db_path)
        if ensure:
            self.schema_report = ensure_schema(self.db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    # -- plumbing ----------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SalesRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _insert(self, table: str, rec: SalesRecord) -> Dict[str, Any]:
        d = rec.to_dict()
        # Empty string FK/reference columns must become NULL so they don't violate
        # a NOT NULL-free but FK-bearing column (sqlite treats '' as a real value
        # that must match a parent PK).
        for k in ("company_id", "prospect_id", "experiment_id", "contact_id", "lead_id"):
            if d.get(k) == "":
                d[k] = None
        cols = list(d.keys())
        vals = [_encode(d[c], c) for c in cols]
        placeholders = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        self._conn.commit()
        return d

    def _update(self, table: str, record_id: str, changes: Dict[str, Any]) -> None:
        if not changes:
            return
        sets = ", ".join(f"{k} = ?" for k in changes)
        vals = [_encode(v, k) for k, v in changes.items()]
        self._conn.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?", [*vals, record_id]
        )
        self._conn.commit()

    def _one(self, cls, table: str, record_id: str):
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        return _decode_row(cls, row) if row else None

    def raw(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Read-only escape hatch for reports/dashboards. Refuses to mutate."""
        head = sql.strip().split(None, 1)[0].lower()
        if head not in ("select", "with", "pragma"):
            raise SalesError(f"raw() is read-only; refusing {head!r} statement")
        return [dict(r) for r in self._conn.execute(sql, params)]

    # -- ICP ---------------------------------------------------------------
    def upsert_icp(self, icp: ICP) -> ICP:
        existing = self._conn.execute(
            "SELECT id FROM icp WHERE name = ?", (icp.name,)
        ).fetchone()
        if existing:
            self._update(
                "icp",
                existing["id"],
                {
                    "industry": icp.industry,
                    "min_team_size": icp.min_team_size,
                    "geography": icp.geography,
                    "rank": icp.rank,
                    "rank_score": icp.rank_score,
                    "offer": icp.offer,
                    "offer_price": icp.offer_price,
                    "source_doc": icp.source_doc,
                    "active": 1 if icp.active else 0,
                },
            )
            got = self._one(ICP, "icp", existing["id"])
            assert got is not None
            return got
        self._insert("icp", icp)
        return icp

    def active_icp(self) -> List[ICP]:
        rows = self._conn.execute(
            "SELECT * FROM icp WHERE active = 1 ORDER BY rank ASC, rank_score DESC"
        ).fetchall()
        return [_decode_row(ICP, r) for r in rows]

    # -- companies / contacts ---------------------------------------------
    def create_company(self, company: Company) -> Company:
        self._insert("companies", company)
        return company

    def get_company(self, company_id: str) -> Optional[Company]:
        return self._one(Company, "companies", company_id)

    def find_company_by_key(self, dedupe_key: str) -> Optional[Company]:
        row = self._conn.execute(
            "SELECT c.* FROM companies c JOIN leads l ON l.company_id = c.id "
            "WHERE l.dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        return _decode_row(Company, row) if row else None

    def create_contact(self, contact: Contact) -> Contact:
        if not self.get_company(contact.company_id):
            raise SalesError(f"no company {contact.company_id!r} to attach a contact to")
        self._insert("contacts", contact)
        return contact

    def get_contact(self, contact_id: str) -> Optional[Contact]:
        return self._one(Contact, "contacts", contact_id)

    def contacts_for(self, company_id: str) -> List[Contact]:
        rows = self._conn.execute(
            "SELECT * FROM contacts WHERE company_id = ? ORDER BY created", (company_id,)
        ).fetchall()
        return [_decode_row(Contact, r) for r in rows]

    # -- leads -------------------------------------------------------------
    def create_lead(
        self,
        company: Company,
        *,
        source: str = "",
        prospect_id: str = "",
        experiment_id: str = "",
        owner: str = "",
    ) -> Dict[str, Any]:
        """Create a lead, deduplicating on the normalised company identity.

        Returns ``{"lead": Lead, "created": bool, "dedupe_key": str}``. An
        existing key is NOT an error — discovery is expected to re-encounter
        companies, and silently creating a duplicate would corrupt the metrics.
        """
        key = normalise_company(company.name, company.domain)
        row = self._conn.execute(
            "SELECT * FROM leads WHERE dedupe_key = ?", (key,)
        ).fetchone()
        if row:
            return {"lead": _decode_row(Lead, row), "created": False, "dedupe_key": key}
        stored = self.get_company(company.id) or self.create_company(company)
        lead = Lead(
            company_id=stored.id,
            prospect_id=prospect_id,
            source=source or company.source,
            dedupe_key=key,
            experiment_id=experiment_id,
            owner=owner,
        )
        self._insert("leads", lead)
        self._insert(
            "pipeline_history",
            PipelineTransition(
                lead_id=lead.id,
                from_stage="",
                to_stage=lead.stage.value,
                reason="lead created",
            ),
        )
        return {"lead": lead, "created": True, "dedupe_key": key}

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        return self._one(Lead, "leads", lead_id)

    def require_lead(self, lead_id: str) -> Lead:
        lead = self.get_lead(lead_id)
        if lead is None:
            raise SalesError(f"no lead {lead_id!r}")
        return lead

    def update_lead(self, lead_id: str, **changes: Any) -> Lead:
        self.require_lead(lead_id)
        allowed = {
            "source", "owner", "experiment_id", "next_action", "next_action_due",
            "lost_reason", "prospect_id",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise SalesError(
                f"lead.update cannot set {sorted(unknown)}; "
                f"stage changes go through pipeline.move and scores through "
                f"qualification.evaluate"
            )
        changes["updated"] = now()
        self._update("leads", lead_id, changes)
        return self.require_lead(lead_id)

    def search_leads(
        self,
        *,
        stage: str = "",
        industry: str = "",
        min_score: float = -1.0,
        query: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = [
            "SELECT l.*, c.name AS company_name, c.industry AS industry,",
            "       c.team_size AS team_size, c.geography AS geography",
            "FROM leads l JOIN companies c ON c.id = l.company_id",
        ]
        clauses, params = [], []
        if stage:
            clauses.append("l.stage = ?")
            params.append(stage_of(stage).value)
        if industry:
            clauses.append("LOWER(c.industry) LIKE ?")
            params.append(f"%{industry.lower()}%")
        if min_score >= 0:
            clauses.append("l.score >= ?")
            params.append(min_score)
        if query:
            clauses.append("(LOWER(c.name) LIKE ? OR LOWER(c.domain) LIKE ?)")
            params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
        if clauses:
            sql.append("WHERE " + " AND ".join(clauses))
        sql.append("ORDER BY l.score DESC, l.created ASC")
        sql.append(f"LIMIT {int(limit)}")
        return [dict(r) for r in self._conn.execute(" ".join(sql), params)]

    # -- pipeline ----------------------------------------------------------
    def move_stage(
        self,
        lead_id: str,
        to_stage: str,
        *,
        reason: str = "",
        run_id: str = "",
        worker: str = "",
    ) -> Dict[str, Any]:
        lead = self.require_lead(lead_id)
        src = stage_of(lead.stage)
        dst = stage_of(to_stage)
        ok, why = can_move(src, dst)
        if not ok:
            raise SalesError(why)
        if dst is PipelineStage.LOST and not reason:
            raise SalesError(
                "moving a lead to 'lost' requires a reason "
                "(CRM_Pipeline.md: 'Lost deals get reason code + re-engage date')"
            )
        changes: Dict[str, Any] = {"stage": dst.value, "updated": now()}
        if dst is PipelineStage.LOST:
            changes["lost_reason"] = reason
        self._update("leads", lead_id, changes)
        transition = PipelineTransition(
            lead_id=lead_id,
            from_stage=src.value,
            to_stage=dst.value,
            reason=reason or why,
            run_id=run_id,
            worker=worker,
        )
        self._insert("pipeline_history", transition)
        self.log_activity(
            Activity(
                lead_id=lead_id,
                kind="stage_change",
                summary=f"{src.value} -> {dst.value}",
                detail=reason or why,
                run_id=run_id,
                worker=worker,
            )
        )
        return {"lead_id": lead_id, "from": src.value, "to": dst.value, "reason": reason or why}

    def stage_history(self, lead_id: str) -> List[PipelineTransition]:
        rows = self._conn.execute(
            "SELECT * FROM pipeline_history WHERE lead_id = ? ORDER BY created ASC",
            (lead_id,),
        ).fetchall()
        return [_decode_row(PipelineTransition, r) for r in rows]

    def pipeline_summary(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM sales_pipeline_summary")]

    def stale_leads(self, *, as_of: Optional[float] = None) -> List[Dict[str, Any]]:
        """Leads past the documented max duration for their current stage."""
        ref = as_of if as_of is not None else time.time()
        out = []
        for row in self._conn.execute(
            "SELECT l.id, l.stage, c.name AS company, "
            "  (SELECT MAX(h.created) FROM pipeline_history h WHERE h.lead_id = l.id) AS entered "
            "FROM leads l JOIN companies c ON c.id = l.company_id"
        ):
            entered = float(row["entered"] or 0)
            if not entered:
                continue
            days = (ref - entered) / 86400.0
            over = days_overdue(row["stage"], days)
            if over > 0:
                out.append(
                    {
                        "lead_id": row["id"],
                        "company": row["company"],
                        "stage": row["stage"],
                        "days_in_stage": round(days, 2),
                        "days_overdue": over,
                    }
                )
        return sorted(out, key=lambda r: r["days_overdue"], reverse=True)

    # -- activities --------------------------------------------------------
    def log_activity(self, activity: Activity) -> Activity:
        self.require_lead(activity.lead_id)
        self._insert("activities", activity)
        return activity

    def activities_for(self, lead_id: str, limit: int = 100) -> List[Activity]:
        rows = self._conn.execute(
            "SELECT * FROM activities WHERE lead_id = ? ORDER BY created DESC LIMIT ?",
            (lead_id, int(limit)),
        ).fetchall()
        return [_decode_row(Activity, r) for r in rows]

    # -- evidence ----------------------------------------------------------
    def attach_evidence(self, ev: SalesEvidenceRecord) -> SalesEvidenceRecord:
        self.require_lead(ev.lead_id)
        if not (ev.source_ref or "").strip():
            raise SalesError(
                "sales evidence requires a source_ref (file#sha256, observation id "
                "or atlas claim id); refusing to store an unsourced claim"
            )
        if ev.claim_type not in SALES_CLAIM_TYPES:
            raise SalesError(
                f"unknown sales claim type {ev.claim_type!r}; valid: {list(SALES_CLAIM_TYPES)}"
            )
        self._insert("sales_evidence", ev)
        return ev

    def evidence_for(self, lead_id: str, claim_type: str = "") -> List[SalesEvidenceRecord]:
        sql = "SELECT * FROM sales_evidence WHERE lead_id = ?"
        params: List[Any] = [lead_id]
        if claim_type:
            sql += " AND claim_type = ?"
            params.append(claim_type)
        sql += " ORDER BY created ASC"
        return [_decode_row(SalesEvidenceRecord, r) for r in self._conn.execute(sql, params)]

    # -- pain points -------------------------------------------------------
    def add_pain_point(self, pp: PainPoint) -> PainPoint:
        self.require_lead(pp.lead_id)
        if not pp.evidence_ids:
            raise SalesError(
                "a pain point must reference at least one evidence id "
                "(no fabricated prospect claims)"
            )
        self._insert("pain_points", pp)
        return pp

    def pain_points_for(self, lead_id: str) -> List[PainPoint]:
        rows = self._conn.execute(
            "SELECT * FROM pain_points WHERE lead_id = ? ORDER BY opportunity_score DESC",
            (lead_id,),
        ).fetchall()
        return [_decode_row(PainPoint, r) for r in rows]

    # -- qualification -----------------------------------------------------
    def next_qualification_version(self, lead_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM qualifications WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        return int(row["v"] or 0) + 1

    def record_qualification(self, qual: Qualification) -> Qualification:
        """Append a scoring record and refresh the lead's cached latest score."""
        self.require_lead(qual.lead_id)
        qual.version = qual.version or self.next_qualification_version(qual.lead_id)
        self._insert("qualifications", qual)
        self._update(
            "leads",
            qual.lead_id,
            {"score": qual.score, "score_version": qual.version, "updated": now()},
        )
        return qual

    def qualifications_for(self, lead_id: str) -> List[Qualification]:
        rows = self._conn.execute(
            "SELECT * FROM qualifications WHERE lead_id = ? ORDER BY version ASC",
            (lead_id,),
        ).fetchall()
        return [_decode_row(Qualification, r) for r in rows]

    def latest_qualification(self, lead_id: str) -> Optional[Qualification]:
        quals = self.qualifications_for(lead_id)
        return quals[-1] if quals else None

    # -- outreach ----------------------------------------------------------
    def create_draft(self, draft: OutreachDraft) -> OutreachDraft:
        self.require_lead(draft.lead_id)
        self._insert("outreach_drafts", draft)
        self.log_activity(
            Activity(
                lead_id=draft.lead_id,
                kind="outreach_draft",
                summary=f"drafted {draft.channel}: {draft.subject[:60]}",
                run_id=draft.run_id,
                evidence_ids=list(draft.evidence_ids),
            )
        )
        return draft

    def get_draft(self, draft_id: str) -> Optional[OutreachDraft]:
        return self._one(OutreachDraft, "outreach_drafts", draft_id)

    def drafts(self, *, state: str = "", lead_id: str = "") -> List[OutreachDraft]:
        sql = "SELECT * FROM outreach_drafts"
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if lead_id:
            clauses.append("lead_id = ?")
            params.append(lead_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created ASC"
        return [_decode_row(OutreachDraft, r) for r in self._conn.execute(sql, params)]

    def approve_draft(self, draft_id: str, by: str) -> OutreachDraft:
        draft = self.get_draft(draft_id)
        if draft is None:
            raise SalesError(f"no outreach draft {draft_id!r}")
        state = draft.state.value if isinstance(draft.state, OutreachState) else draft.state
        if state != OutreachState.DRAFT.value:
            raise SalesError(
                f"draft {draft_id} is {state!r} — only a 'draft' can be approved"
            )
        if not by:
            raise SalesError("approving outreach requires an approver identity")
        self._update(
            "outreach_drafts",
            draft_id,
            {"state": OutreachState.APPROVED.value, "approved_by": by, "approved_at": now()},
        )
        got = self.get_draft(draft_id)
        assert got is not None
        return got

    def record_sent(
        self, draft_id: str, *, receipt: str = "", experiment_id: str = ""
    ) -> Dict[str, Any]:
        """Record that an approved draft was actually delivered.

        Refuses an unapproved draft: this is the domain-level backstop behind the
        policy gate, so 'send' cannot happen through a code path that skipped
        approval.
        """
        draft = self.get_draft(draft_id)
        if draft is None:
            raise SalesError(f"no outreach draft {draft_id!r}")
        state = draft.state.value if isinstance(draft.state, OutreachState) else draft.state
        if state != OutreachState.APPROVED.value:
            raise SalesError(
                f"refusing to record a send for draft {draft_id}: state is {state!r}, "
                "not 'approved' (external actions require approval first)"
            )
        self._update(
            "outreach_drafts",
            draft_id,
            {"state": OutreachState.SENT.value, "sent_at": now(), "receipt": receipt},
        )
        exp = experiment_id or draft.experiment_id
        # Feed the pre-existing DailySalesOS experiment tables, so the learning
        # layer keeps working exactly as before — but only when the referenced
        # experiment/prospect actually exists (those rows are owned by DailySalesOS,
        # not by this layer, so we never fabricate them).
        if draft.lead_id and exp:
            lead = self.get_lead(draft.lead_id)
            prospect_id = lead.prospect_id if lead else ""
            exp_exists = bool(self._conn.execute(
                "SELECT 1 FROM experiments WHERE id = ?", (exp,)
            ).fetchone())
            if exp_exists and prospect_id:
                self._conn.execute(
                    "INSERT INTO outreach_touches "
                    "(prospect_id, experiment_id, sent_at, channel, message_variant, status) "
                    "VALUES (?, ?, ?, ?, ?, 'sent')",
                    (
                        prospect_id,
                        exp,
                        datetime.now(timezone.utc).isoformat(),
                        draft.channel,
                        draft.variant,
                    ),
                )
            if exp_exists:
                self._conn.execute(
                    "INSERT INTO experiment_metrics (experiment_id, date, sent) "
                    "VALUES (?, ?, 1) "
                    "ON CONFLICT(experiment_id, date) DO UPDATE SET sent = sent + 1",
                    (exp, _today()),
                )
            self._conn.commit()
        self.log_activity(
            Activity(
                lead_id=draft.lead_id,
                kind="outreach",
                summary=f"sent {draft.channel}: {draft.subject[:60]}",
                detail=receipt,
                run_id=draft.run_id,
            )
        )
        return {"draft_id": draft_id, "state": "sent", "receipt": receipt, "experiment_id": exp}

    # -- tasks / follow-ups ------------------------------------------------
    def create_task(self, task: Task) -> Task:
        self.require_lead(task.lead_id)
        self._insert("tasks", task)
        return task

    def complete_task(self, task_id: str) -> Task:
        task = self._one(Task, "tasks", task_id)
        if task is None:
            raise SalesError(f"no task {task_id!r}")
        self._update(
            "tasks", task_id, {"state": TaskState.DONE.value, "completed_at": now()}
        )
        got = self._one(Task, "tasks", task_id)
        assert got is not None
        return got

    def open_tasks(self, lead_id: str = "") -> List[Task]:
        sql = "SELECT * FROM tasks WHERE state = 'open'"
        params: List[Any] = []
        if lead_id:
            sql += " AND lead_id = ?"
            params.append(lead_id)
        sql += " ORDER BY due ASC"
        return [_decode_row(Task, r) for r in self._conn.execute(sql, params)]

    def schedule_followup(self, fu: FollowUp) -> FollowUp:
        self.require_lead(fu.lead_id)
        try:
            date.fromisoformat(fu.due)
        except ValueError:
            raise SalesError(f"follow-up due date must be ISO YYYY-MM-DD, got {fu.due!r}")
        self._insert("followups", fu)
        self._update(
            "leads",
            fu.lead_id,
            {"next_action": fu.reason or fu.step, "next_action_due": fu.due, "updated": now()},
        )
        return fu

    def due_followups(self, on: str = "") -> List[FollowUp]:
        day = on or _today()
        rows = self._conn.execute(
            "SELECT * FROM followups WHERE state = 'open' AND due <= ? ORDER BY due ASC",
            (day,),
        ).fetchall()
        return [_decode_row(FollowUp, r) for r in rows]

    def complete_followup(self, fu_id: str) -> FollowUp:
        fu = self._one(FollowUp, "followups", fu_id)
        if fu is None:
            raise SalesError(f"no follow-up {fu_id!r}")
        self._update(
            "followups", fu_id, {"state": TaskState.DONE.value, "completed_at": now()}
        )
        got = self._one(FollowUp, "followups", fu_id)
        assert got is not None
        return got

    # -- opportunities / proposals / outcomes ------------------------------
    def create_opportunity(self, opp: Opportunity) -> Opportunity:
        self.require_lead(opp.lead_id)
        self._insert("opportunities", opp)
        return opp

    def update_opportunity(self, opp_id: str, **changes: Any) -> Opportunity:
        opp = self._one(Opportunity, "opportunities", opp_id)
        if opp is None:
            raise SalesError(f"no opportunity {opp_id!r}")
        allowed = {"name", "value", "probability", "close_date", "stage"}
        unknown = set(changes) - allowed
        if unknown:
            raise SalesError(f"opportunity.update cannot set {sorted(unknown)}")
        if "stage" in changes:
            changes["stage"] = stage_of(changes["stage"]).value
        changes["updated"] = now()
        self._update("opportunities", opp_id, changes)
        got = self._one(Opportunity, "opportunities", opp_id)
        assert got is not None
        return got

    def opportunities_for(self, lead_id: str) -> List[Opportunity]:
        rows = self._conn.execute(
            "SELECT * FROM opportunities WHERE lead_id = ? ORDER BY created", (lead_id,)
        ).fetchall()
        return [_decode_row(Opportunity, r) for r in rows]

    def create_proposal(self, prop: Proposal) -> Proposal:
        self.require_lead(prop.lead_id)
        self._insert("proposals", prop)
        return prop

    def record_outcome(self, outcome: Outcome) -> Outcome:
        self.require_lead(outcome.lead_id)
        if outcome.result not in ("won", "lost"):
            raise SalesError(f"outcome.result must be 'won' or 'lost', got {outcome.result!r}")
        self._insert("outcomes", outcome)
        return outcome

    # -- lead detail (for UI / API) ---------------------------------------
    def lead_detail(self, lead_id: str) -> Dict[str, Any]:
        lead = self.require_lead(lead_id)
        company = self.get_company(lead.company_id)
        qual = self.latest_qualification(lead_id)
        return {
            "lead": lead.to_dict(),
            "company": company.to_dict() if company else {},
            "contacts": [c.to_dict() for c in self.contacts_for(lead.company_id)],
            "qualification": qual.to_dict() if qual else {},
            "qualification_history": [q.to_dict() for q in self.qualifications_for(lead_id)],
            "pain_points": [p.to_dict() for p in self.pain_points_for(lead_id)],
            "evidence": [e.to_dict() for e in self.evidence_for(lead_id)],
            "activities": [a.to_dict() for a in self.activities_for(lead_id)],
            "outreach": [d.to_dict() for d in self.drafts(lead_id=lead_id)],
            "tasks": [t.to_dict() for t in self.open_tasks(lead_id)],
            "followups": [
                f.to_dict()
                for f in [
                    _decode_row(FollowUp, r)
                    for r in self._conn.execute(
                        "SELECT * FROM followups WHERE lead_id = ? ORDER BY due", (lead_id,)
                    )
                ]
            ],
            "pipeline_history": [t.to_dict() for t in self.stage_history(lead_id)],
            "opportunities": [o.to_dict() for o in self.opportunities_for(lead_id)],
        }
