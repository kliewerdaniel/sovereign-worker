"""Extension of the existing DailySalesOS ``Experiment_Ledger`` schema.

Additive and idempotent. The pre-existing tables — ``experiments``,
``experiment_metrics``, ``prospects``, ``outreach_touches``, ``deals`` and the
``experiment_summary`` / ``daily_activity`` views — are never dropped, altered or
rewritten; the sales ontology is added alongside them and ``leads.prospect_id``
references ``prospects(id)`` so the pre-existing prospect corpus stays the origin
of record.

There is deliberately no second database: this is the DailySalesOS data layer,
and sworker's own run/evidence/approval state stays in ``.state/worker.db`` +
``audit.jsonl``.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List

SALES_SCHEMA_VERSION = 1

# Tables owned by the sales domain layer. Order matters (foreign keys).
SALES_TABLES: List[str] = [
    "companies",
    "contacts",
    "leads",
    "activities",
    "opportunities",
    "pipeline_history",
    "qualifications",
    "sales_evidence",
    "pain_points",
    "outreach_drafts",
    "tasks",
    "followups",
    "icp",
    "proposals",
    "outcomes",
]

# Tables that already existed and must survive untouched.
# Pre-existing DailySalesOS tables this layer references (FK targets + the
# experiment learning tables fed by record_sent). When a fresh ledger is created
# we materialise minimal stubs IF NOT EXISTS so the FK graph closes and the
# learning layer keeps working — but an already-populated DailySalesOS ledger is
# left exactly as-is. These DDL strings mirror the real schemas in
# Experiment_Ledger/experiments.db so the stub is schema-compatible with any
# existing db.
_PREEXISTING_DDL = {
    "experiments": """
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            status TEXT CHECK(status IN ('active','completed','paused','killed')),
            hypothesis TEXT NOT NULL,
            segment_industry TEXT, segment_role TEXT, segment_team_size TEXT,
            segment_geography TEXT, message_variant TEXT, offer_variant TEXT,
            channel TEXT, sample_size_target INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "prospects": """
        CREATE TABLE IF NOT EXISTS prospects (
            id TEXT PRIMARY KEY, company_name TEXT, contact_name TEXT, email TEXT,
            industry TEXT, role TEXT, team_size INTEGER, geography TEXT, source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "outreach_touches": """
        CREATE TABLE IF NOT EXISTS outreach_touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id TEXT REFERENCES prospects(id),
            experiment_id TEXT REFERENCES experiments(id),
            sent_at TEXT NOT NULL, channel TEXT, message_variant TEXT,
            status TEXT CHECK(status IN ('sent','delivered','opened','replied','positive','negative'))
        )
    """,
    "experiment_metrics": """
        CREATE TABLE IF NOT EXISTS experiment_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT REFERENCES experiments(id),
            date TEXT NOT NULL, sent INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0,
            opened INTEGER DEFAULT 0, replies INTEGER DEFAULT 0, positive_replies INTEGER DEFAULT 0,
            discoveries_booked INTEGER DEFAULT 0, qualified INTEGER DEFAULT 0,
            audits_closed INTEGER DEFAULT 0, implementations_closed INTEGER DEFAULT 0,
            revenue_closed REAL DEFAULT 0, UNIQUE(experiment_id, date)
        )
    """,
}

PREEXISTING_TABLES: List[str] = list(_PREEXISTING_DDL)

_DDL: Dict[str, str] = {
    "companies": """
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            domain TEXT,
            industry TEXT,
            geography TEXT,
            team_size INTEGER DEFAULT 0,
            description TEXT,
            website TEXT,
            source TEXT,
            created REAL,
            updated REAL
        )
    """,
    "contacts": """
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY,
            company_id TEXT REFERENCES companies(id),
            name TEXT NOT NULL,
            role TEXT,
            email TEXT,
            phone TEXT,
            is_decision_maker INTEGER DEFAULT 0,
            source TEXT,
            created REAL
        )
    """,
    "leads": """
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            company_id TEXT REFERENCES companies(id),
            prospect_id TEXT REFERENCES prospects(id),
            stage TEXT NOT NULL,
            source TEXT,
            dedupe_key TEXT UNIQUE,
            score REAL DEFAULT 0,
            score_version INTEGER DEFAULT 0,
            owner TEXT,
            experiment_id TEXT REFERENCES experiments(id),
            lost_reason TEXT,
            next_action TEXT,
            next_action_due TEXT,
            created REAL,
            updated REAL
        )
    """,
    "activities": """
        CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            run_id TEXT,
            worker TEXT,
            detail TEXT,
            evidence_ids TEXT,
            created REAL
        )
    """,
    "opportunities": """
        CREATE TABLE IF NOT EXISTS opportunities (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            name TEXT,
            value REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            stage TEXT,
            probability REAL DEFAULT 0,
            close_date TEXT,
            created REAL,
            updated REAL
        )
    """,
    "pipeline_history": """
        CREATE TABLE IF NOT EXISTS pipeline_history (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            from_stage TEXT,
            to_stage TEXT NOT NULL,
            reason TEXT,
            run_id TEXT,
            worker TEXT,
            created REAL
        )
    """,
    "qualifications": """
        CREATE TABLE IF NOT EXISTS qualifications (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            version INTEGER NOT NULL,
            icp_fit REAL DEFAULT 0,
            pain_signal REAL DEFAULT 0,
            urgency REAL DEFAULT 0,
            economic_potential REAL DEFAULT 0,
            accessibility REAL DEFAULT 0,
            confidence REAL DEFAULT 0,
            score REAL DEFAULT 0,
            tier TEXT,
            signals TEXT,
            evidence_ids TEXT,
            reasoning TEXT,
            model TEXT,
            model_version TEXT,
            run_id TEXT,
            created REAL,
            UNIQUE(lead_id, version)
        )
    """,
    "sales_evidence": """
        CREATE TABLE IF NOT EXISTS sales_evidence (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            claim_type TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            tier TEXT,
            excerpt TEXT,
            run_id TEXT,
            observation_id TEXT,
            worker_evidence_id TEXT,
            confidence REAL DEFAULT 0,
            created REAL
        )
    """,
    "pain_points": """
        CREATE TABLE IF NOT EXISTS pain_points (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            text TEXT NOT NULL,
            category TEXT,
            severity INTEGER DEFAULT 0,
            frequency INTEGER DEFAULT 0,
            revenue_impact INTEGER DEFAULT 0,
            automation_potential INTEGER DEFAULT 0,
            implementation_difficulty INTEGER DEFAULT 1,
            opportunity_score REAL DEFAULT 0,
            tier TEXT,
            evidence_ids TEXT,
            created REAL
        )
    """,
    "outreach_drafts": """
        CREATE TABLE IF NOT EXISTS outreach_drafts (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            contact_id TEXT REFERENCES contacts(id),
            channel TEXT NOT NULL,
            subject TEXT,
            body TEXT NOT NULL,
            state TEXT NOT NULL,
            sequence_step TEXT,
            variant TEXT,
            experiment_id TEXT REFERENCES experiments(id),
            evidence_ids TEXT,
            approved_by TEXT,
            approved_at REAL DEFAULT 0,
            sent_at REAL DEFAULT 0,
            receipt TEXT,
            run_id TEXT,
            created REAL
        )
    """,
    "tasks": """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            title TEXT NOT NULL,
            kind TEXT,
            due TEXT,
            state TEXT NOT NULL,
            detail TEXT,
            run_id TEXT,
            completed_at REAL DEFAULT 0,
            created REAL
        )
    """,
    "followups": """
        CREATE TABLE IF NOT EXISTS followups (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            due TEXT NOT NULL,
            reason TEXT,
            sequence TEXT,
            step TEXT,
            state TEXT NOT NULL,
            run_id TEXT,
            completed_at REAL DEFAULT 0,
            created REAL
        )
    """,
    "icp": """
        CREATE TABLE IF NOT EXISTS icp (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            industry TEXT,
            min_team_size INTEGER DEFAULT 0,
            geography TEXT,
            rank INTEGER DEFAULT 0,
            rank_score REAL DEFAULT 0,
            offer TEXT,
            offer_price REAL DEFAULT 0,
            source_doc TEXT,
            active INTEGER DEFAULT 1,
            created REAL
        )
    """,
    "proposals": """
        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            value REAL DEFAULT 0,
            sent_date TEXT,
            state TEXT,
            detail TEXT,
            created REAL
        )
    """,
    "outcomes": """
        CREATE TABLE IF NOT EXISTS outcomes (
            id TEXT PRIMARY KEY,
            lead_id TEXT REFERENCES leads(id),
            result TEXT NOT NULL,
            reason TEXT,
            value REAL DEFAULT 0,
            revisit_date TEXT,
            created REAL
        )
    """,
}

_INDEXES: List[str] = [
    "CREATE INDEX IF NOT EXISTS ix_leads_stage ON leads(stage)",
    "CREATE INDEX IF NOT EXISTS ix_leads_company ON leads(company_id)",
    "CREATE INDEX IF NOT EXISTS ix_leads_score ON leads(score)",
    "CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_id)",
    "CREATE INDEX IF NOT EXISTS ix_activities_lead ON activities(lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_activities_created ON activities(created)",
    "CREATE INDEX IF NOT EXISTS ix_sev_lead ON sales_evidence(lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_qual_lead ON qualifications(lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_pp_lead ON pain_points(lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_out_lead ON outreach_drafts(lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_out_state ON outreach_drafts(state)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_state ON tasks(state)",
    "CREATE INDEX IF NOT EXISTS ix_fu_state ON followups(state)",
    "CREATE INDEX IF NOT EXISTS ix_fu_due ON followups(due)",
    "CREATE INDEX IF NOT EXISTS ix_ph_lead ON pipeline_history(lead_id)",
]

# Convenience views over the sales tables. Named with a sales_ prefix so they can
# never collide with the pre-existing experiment_summary / daily_activity views.
_VIEWS: List[str] = [
    """
    CREATE VIEW IF NOT EXISTS sales_pipeline_summary AS
    SELECT l.stage AS stage,
           COUNT(*) AS leads,
           ROUND(AVG(l.score), 2) AS avg_score,
           COALESCE(SUM(o.value), 0) AS pipeline_value
    FROM leads l
    LEFT JOIN opportunities o ON o.lead_id = l.id
    GROUP BY l.stage
    """,
    """
    CREATE VIEW IF NOT EXISTS sales_lead_overview AS
    SELECT l.id AS lead_id,
           c.name AS company,
           c.industry AS industry,
           l.stage AS stage,
           l.score AS score,
           l.next_action AS next_action,
           l.next_action_due AS next_action_due,
           (SELECT COUNT(*) FROM sales_evidence e WHERE e.lead_id = l.id) AS evidence_count,
           (SELECT MAX(a.created) FROM activities a WHERE a.lead_id = l.id) AS last_activity
    FROM leads l
    JOIN companies c ON c.id = l.company_id
    """,
]


def _table_names(conn: sqlite3.Connection) -> List[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    ]


def ensure_schema(db_path: str) -> Dict[str, object]:
    """Create the sales tables in an existing (or new) Experiment_Ledger db.

    Never destructive: only ``CREATE ... IF NOT EXISTS``. Returns a report of
    what was added and what pre-existing tables were found, so the caller can
    prove nothing was replaced.
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        before = set(_table_names(conn))
        conn.execute("PRAGMA foreign_keys = ON")
        for table in SALES_TABLES:
            conn.execute(_DDL[table])
        for stmt in _PREEXISTING_DDL.values():
            conn.execute(stmt)
        for stmt in _INDEXES:
            conn.execute(stmt)
        for stmt in _VIEWS:
            conn.execute(stmt)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sales_meta (k TEXT PRIMARY KEY, v TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO sales_meta (k, v) VALUES ('schema_version', ?)",
            (str(SALES_SCHEMA_VERSION),),
        )
        conn.commit()
        after = set(_table_names(conn))
    finally:
        conn.close()
    return {
        "db": os.path.abspath(db_path),
        "schema_version": SALES_SCHEMA_VERSION,
        "created": sorted(after - before),
        "preexisting": sorted(t for t in PREEXISTING_TABLES if t in before),
        "sales_tables": list(SALES_TABLES),
    }
