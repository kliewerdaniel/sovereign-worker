"""Daily metrics — activity measured against DailySalesOS's own targets.

The targets come from ``Metrics_Single_Source_of_Truth.md`` (parsed by
``sales/knowledge.py``), never from constants in this file. That document says
"All documents reference this file for target numbers", and this module is one of
those documents.

Every count is a SQL count over the sales tables, so the daily report is
re-derivable — which is what the ``sales_metrics_match_ledger`` verification
check independently confirms after a run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .repository import SalesRepository


def day_bounds(day: str = "") -> tuple:
    """UTC epoch bounds for an ISO day (defaults to today)."""
    if day:
        d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        d = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start = d.timestamp()
    return start, start + 86400.0


def daily_counts(repo: SalesRepository, day: str = "") -> Dict[str, int]:
    """Raw activity counts for one day, straight out of the ledger."""
    start, end = day_bounds(day)
    q = lambda sql: int(repo.raw(sql, (start, end))[0]["n"])  # noqa: E731
    return {
        "leads_discovered": q(
            "SELECT COUNT(*) AS n FROM leads WHERE created >= ? AND created < ?"
        ),
        "leads_researched": q(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM sales_evidence "
            "WHERE created >= ? AND created < ?"
        ),
        "leads_qualified": q(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM qualifications "
            "WHERE created >= ? AND created < ?"
        ),
        "outreach_drafted": q(
            "SELECT COUNT(*) AS n FROM outreach_drafts WHERE created >= ? AND created < ?"
        ),
        "outreach_sent": q(
            "SELECT COUNT(*) AS n FROM outreach_drafts WHERE sent_at >= ? AND sent_at < ?"
        ),
        "followups_sent": q(
            "SELECT COUNT(*) AS n FROM followups WHERE completed_at >= ? AND completed_at < ?"
        ),
        "followups_scheduled": q(
            "SELECT COUNT(*) AS n FROM followups WHERE created >= ? AND created < ?"
        ),
        "replies": q(
            "SELECT COUNT(*) AS n FROM activities WHERE kind = 'reply' "
            "AND created >= ? AND created < ?"
        ),
        "discoveries_scheduled": q(
            "SELECT COUNT(*) AS n FROM pipeline_history "
            "WHERE to_stage = 'discovery_scheduled' AND created >= ? AND created < ?"
        ),
        "discoveries_completed": q(
            "SELECT COUNT(*) AS n FROM pipeline_history "
            "WHERE to_stage = 'discovery_completed' AND created >= ? AND created < ?"
        ),
        "proposals_sent": q(
            "SELECT COUNT(*) AS n FROM pipeline_history "
            "WHERE to_stage = 'proposal_sent' AND created >= ? AND created < ?"
        ),
        "won": q(
            "SELECT COUNT(*) AS n FROM pipeline_history WHERE to_stage = 'won' "
            "AND created >= ? AND created < ?"
        ),
        "lost": q(
            "SELECT COUNT(*) AS n FROM pipeline_history WHERE to_stage = 'lost' "
            "AND created >= ? AND created < ?"
        ),
    }


# counts key -> target key from Metrics_Single_Source_of_Truth.md
TARGET_MAP = {
    "prospects_researched": "leads_researched",
    "outreach_sent": "outreach_sent",
    "followups_sent": "followups_sent",
    "discoveries_completed": "discoveries_completed",
    "discoveries_scheduled": "discoveries_scheduled",
}


def conversion_rates(counts: Dict[str, int]) -> Dict[str, Optional[float]]:
    def pct(num: int, den: int) -> Optional[float]:
        return round(num / den * 100.0, 2) if den else None

    return {
        "reply_rate": pct(counts["replies"], counts["outreach_sent"]),
        "discovery_booking_rate": pct(counts["discoveries_scheduled"], counts["replies"]),
        "qualified_rate": pct(counts["leads_qualified"], counts["leads_researched"]),
        "proposal_rate": pct(counts["proposals_sent"], counts["leads_qualified"]),
        "win_rate": pct(counts["won"], counts["proposals_sent"]),
    }


def bottlenecks(repo: SalesRepository, counts: Dict[str, int], targets: Dict[str, int]) -> List[str]:
    out: List[str] = []
    for tkey, ckey in TARGET_MAP.items():
        target = targets.get(tkey)
        if target is None:
            continue
        actual = counts.get(ckey, 0)
        if actual < target:
            out.append(f"{ckey}: {actual} of {target} target ({tkey})")
    stale = repo.stale_leads()
    if stale:
        worst = stale[0]
        out.append(
            f"{len(stale)} lead(s) past their stage SLA; worst: {worst['company']} "
            f"in {worst['stage']} for {worst['days_in_stage']}d "
            f"({worst['days_overdue']}d over)"
        )
    pending = repo.drafts(state="draft")
    if pending:
        out.append(f"{len(pending)} outreach draft(s) awaiting approval")
    return out


def daily_report(
    repo: SalesRepository,
    *,
    targets: Optional[Dict[str, int]] = None,
    targets_source: str = "",
    day: str = "",
) -> Dict[str, Any]:
    """The full daily report. Pure function of the ledger + the targets doc."""
    counts = daily_counts(repo, day)
    tgt = targets or {}
    vs_target = {}
    met_all = bool(tgt)
    for tkey, ckey in TARGET_MAP.items():
        if tkey not in tgt:
            continue
        actual = counts.get(ckey, 0)
        met = actual >= tgt[tkey]
        met_all = met_all and met
        vs_target[tkey] = {
            "metric": ckey,
            "actual": actual,
            "target": tgt[tkey],
            "met": met,
        }
    return {
        "date": day or datetime.now(timezone.utc).date().isoformat(),
        "counts": counts,
        "targets": tgt,
        "targets_source": targets_source,
        "vs_target": vs_target,
        # Metrics_Single_Source_of_Truth.md: "If all not met -> Failed sales day."
        "failed_sales_day": (not met_all) if tgt else None,
        "conversion_rates": conversion_rates(counts),
        "pipeline": repo.pipeline_summary(),
        "bottlenecks": bottlenecks(repo, counts, tgt),
        "pending_approvals": len(repo.drafts(state="draft")),
    }


def render_markdown(report: Dict[str, Any]) -> str:
    """Human-readable daily sales report artifact."""
    lines = [
        f"# Daily Sales Report — {report['date']}",
        "",
        "## Activity vs targets",
        "",
        "| Target (Metrics_Single_Source_of_Truth.md) | Metric | Actual | Target | Met |",
        "|---|---|---|---|---|",
    ]
    if report["vs_target"]:
        for tkey, row in report["vs_target"].items():
            lines.append(
                f"| {tkey} | {row['metric']} | {row['actual']} | {row['target']} | "
                f"{'yes' if row['met'] else 'NO'} |"
            )
    else:
        lines.append("| (no targets parsed from the metrics document) | - | - | - | - |")
    if report.get("failed_sales_day") is True:
        lines += ["", "**Failed sales day** — not all daily minimums were met."]
    elif report.get("failed_sales_day") is False:
        lines += ["", "All daily minimums met."]

    lines += ["", "## Counts", ""]
    for k, v in report["counts"].items():
        lines.append(f"- {k}: {v}")

    lines += ["", "## Conversion rates", ""]
    for k, v in report["conversion_rates"].items():
        lines.append(f"- {k}: {'n/a' if v is None else f'{v}%'}")

    lines += ["", "## Pipeline", "", "| Stage | Leads | Avg score | Value |", "|---|---|---|---|"]
    for row in report["pipeline"]:
        lines.append(
            f"| {row.get('stage')} | {row.get('leads')} | {row.get('avg_score')} | "
            f"{row.get('pipeline_value')} |"
        )

    lines += ["", "## Bottlenecks", ""]
    if report["bottlenecks"]:
        lines += [f"- {b}" for b in report["bottlenecks"]]
    else:
        lines.append("- none detected")

    lines += [
        "",
        f"Outreach drafts awaiting approval: {report['pending_approvals']}",
        "",
        "Every number above is a count over the Experiment_Ledger sales tables and "
        "is re-derivable with `sworker verify` (check: sales_metrics_match_ledger).",
        "",
    ]
    return "\n".join(lines)
