"""parse_daily_targets must read the live DailySalesOS single-source-of-truth.

This is a regression guard: the daily "Failed sales day" badge is computed against
whatever Metrics_Single_Source_of_Truth.md actually says. If the doc's wording
drifts, the patterns here must break loudly instead of silently degrading to a
non-zero but wrong target. Verified against the real file on 2026-08-11.
"""

from __future__ import annotations

import os

from sworker.sales import knowledge

DAILYSALESOS = os.path.expanduser("~/Documents/Projects/salesworkflow")


def test_parse_daily_targets_real_doc():
    res = knowledge.parse_daily_targets(DAILYSALESOS)
    assert res["found"] is True, "Metrics_Single_Source_of_Truth.md not found at DAILYSALESOS root"
    assert res["source_doc"] == "Metrics_Single_Source_of_Truth.md"

    # Pinned to the live document as of 2026-08-11 (lines 6-10).
    assert res["targets"] == {
        "prospects_researched": 20,
        "outreach_sent": 15,
        "followups_sent": 10,
        "discoveries_completed": 1,
        "discoveries_scheduled": 2,
    }, res["targets"]

    # Every target carries a line-level source ref (attributability invariant).
    assert res["refs"] == {
        "prospects_researched": "Metrics_Single_Source_of_Truth.md:6",
        "outreach_sent": "Metrics_Single_Source_of_Truth.md:7",
        "followups_sent": "Metrics_Single_Source_of_Truth.md:8",
        "discoveries_completed": "Metrics_Single_Source_of_Truth.md:9",
        "discoveries_scheduled": "Metrics_Single_Source_of_Truth.md:10",
    }, res["refs"]


def test_parse_daily_targets_absent_degrades_not_fabricates():
    # With no source doc present, targets are empty and found=False — never invented.
    res = knowledge.parse_daily_targets("/nonexistent/path")
    assert res["found"] is False
    assert res["targets"] == {}
    assert res["source_doc"] == ""
