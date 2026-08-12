"""Sales verification checks (re-registered into sworker's verify engine).

These are deterministic re-derivations over the Experiment_Ledger, exactly like
the checks in ``sworker/verify.py``. They are registered with the same ``@check``
decorator so a DAILY_SALES_RUN procedure can carry them and a run degrades to
PARTIAL_SUCCESS if any fails. The engine never trusts the stored number — it
recomputes it from the row data.

Checks:
    sales_score_recomputes        recompute a lead's weighted score from its stored sub-scores
    sales_evidence_has_source     every stored sales claim carries a non-empty source_ref
    sales_metrics_match_ledger    daily counts recomputed == counts reported by metrics.daily_counts
    sales_outreach_approved_first every 'sent' draft has an 'approved' ancestor (no egress without approval)
    sales_pipeline_legal         every lead's stage is a documented CRM_Pipeline.md stage
"""

from __future__ import annotations

from typing import Any, Dict

from ..models import VerificationOutcome
from ..verify import check
from .pipeline import STAGES, stage_of
from .qualification import WEIGHTS
from .repository import SalesRepository, default_ledger_path


def _open(workspace: str) -> SalesRepository:
    return SalesRepository(default_ledger_path(workspace))


@check("sales_score_recomputes")
def _score_recomputes(spec: Dict[str, Any], workspace: str) -> Any:
    from .qualification import score_breakdown

    repo = _open(workspace)
    lead_id = spec.get("lead_id")
    if lead_id:
        leads = [repo.require_lead(lead_id)]
    else:
        leads = [
            repo.require_lead(r["id"])
            for r in repo.raw("SELECT id FROM leads")
        ]
    mismatches = []
    for lead in leads:
        br = score_breakdown(repo, lead.id)
        if not br["found"]:
            continue
        if not br["matches"]:
            mismatches.append(
                {"lead_id": lead.id, "stored": br["stored_score"], "recomputed": br["recomputed_score"]}
            )
    if mismatches:
        return CheckResult(
            check="sales_score_recomputes",
            status=VerificationOutcome.FAIL,
            detail=f"{len(mismatches)} lead(s) where stored score != recomputed: {mismatches}",
            actual=mismatches,
        )
    return CheckResult(
        check="sales_score_recomputes",
        status=VerificationOutcome.PASS,
        detail=f"recomputed {len(leads)} lead score(s); all match stored totals",
    )


@check("sales_evidence_has_source")
def _evidence_source(spec: Dict[str, Any], workspace: str) -> Any:
    repo = _open(workspace)
    bad = repo.raw(
        "SELECT id, claim_type FROM sales_evidence WHERE source_ref IS NULL OR TRIM(source_ref) = ''"
    )
    if bad:
        return CheckResult(
            check="sales_evidence_has_source",
            status=VerificationOutcome.FAIL,
            detail=f"{len(bad)} sales evidence row(s) without a source_ref: {[b['id'] for b in bad]}",
            actual=bad,
        )
    return CheckResult(
        check="sales_evidence_has_source",
        status=VerificationOutcome.PASS,
        detail="all sales evidence rows carry a source_ref",
    )


@check("sales_outreach_approved_first")
def _approved_first(spec: Dict[str, Any], workspace: str) -> Any:
    repo = _open(workspace)
    bad = repo.raw(
        "SELECT id FROM outreach_drafts WHERE state = 'sent' AND (approved_by IS NULL OR TRIM(approved_by) = '')"
    )
    if bad:
        return CheckResult(
            check="sales_outreach_approved_first",
            status=VerificationOutcome.FAIL,
            detail=f"{len(bad)} sent draft(s) with no approver: {[b['id'] for b in bad]}",
            actual=bad,
        )
    return CheckResult(
        check="sales_outreach_approved_first",
        status=VerificationOutcome.PASS,
        detail="every sent draft was approved first",
    )


@check("sales_pipeline_legal")
def _pipeline_legal(spec: Dict[str, Any], workspace: str) -> Any:
    repo = _open(workspace)
    valid = {s.value for s in STAGES}
    bad = repo.raw(
        f"SELECT id, stage FROM leads WHERE stage NOT IN ({','.join('?' for _ in valid)})",
        tuple(valid),
    )
    if bad:
        return CheckResult(
            check="sales_pipeline_legal",
            status=VerificationOutcome.FAIL,
            detail=f"{len(bad)} lead(s) in undocumented stage(s): {bad}",
            actual=bad,
        )
    return CheckResult(
        check="sales_pipeline_legal",
        status=VerificationOutcome.PASS,
        detail="all leads are in documented CRM_Pipeline.md stages",
    )


@check("sales_metrics_match_ledger")
def _metrics_match(spec: Dict[str, Any], workspace: str) -> Any:
    """Re-derive daily counts from the ledger and compare to a reported value.

    The procedure passes the day and an ``expect`` dict of metric->count; if any
    differs from a fresh recomputation, the check fails.
    """
    from .metrics import daily_counts

    repo = _open(workspace)
    day = spec.get("day", "")
    recomputed = daily_counts(repo, day)
    expect = spec.get("expect", {})
    if not expect:
        return CheckResult(
            check="sales_metrics_match_ledger",
            status=VerificationOutcome.PASS,
            detail="no expected metrics supplied; recomputed only",
            actual=recomputed,
        )
    diffs = {
        k: {"expected": expect[k], "actual": recomputed.get(k)}
        for k in expect
        if recomputed.get(k) != expect[k]
    }
    if diffs:
        return CheckResult(
            check="sales_metrics_match_ledger",
            status=VerificationOutcome.FAIL,
            detail=f"metric recomputation diverged from reported: {diffs}",
            actual=diffs,
        )
    return CheckResult(
        check="sales_metrics_match_ledger",
        status=VerificationOutcome.PASS,
        detail="recomputed daily metrics match reported counts",
    )


# Local import of CheckResult to avoid a circular import at module top.
from ..verify import CheckResult  # noqa: E402

__all__ = [
    "sales_checks",
]

sales_checks = [
    "sales_score_recomputes",
    "sales_evidence_has_source",
    "sales_outreach_approved_first",
    "sales_pipeline_legal",
    "sales_metrics_match_ledger",
]
