"""Sales domain layer — DailySalesOS × Sovereign Worker (§71).

DailySalesOS owns the sales domain (markdown knowledge + the Experiment_Ledger
sqlite database). Sovereign Worker owns execution (engine, permissions, evidence,
verification, procedures, audit). This package is the boundary between them.

Nothing here re-implements a sworker subsystem: the five-tier permission model,
the EvidenceLedger, verify.py's deterministic checks, procedures.py, scheduler.py
and the sqlite+audit.jsonl store are all reused as-is. See
``docs/SALES_INTEGRATION.md``.
"""

from __future__ import annotations

from .models import (
    Activity,
    ClaimTier,
    Company,
    Contact,
    FollowUp,
    Lead,
    Opportunity,
    OutreachDraft,
    PainPoint,
    PipelineStage,
    Qualification,
    SalesEvidenceRecord,
    Task,
)
from .pipeline import STAGES, can_move, stage_index, stage_of
from .repository import SalesRepository, default_ledger_path
from .schema import ensure_schema

__all__ = [
    "Activity",
    "ClaimTier",
    "Company",
    "Contact",
    "FollowUp",
    "Lead",
    "Opportunity",
    "OutreachDraft",
    "PainPoint",
    "PipelineStage",
    "Qualification",
    "SalesEvidenceRecord",
    "Task",
    "STAGES",
    "can_move",
    "stage_index",
    "stage_of",
    "SalesRepository",
    "default_ledger_path",
    "ensure_schema",
]
