"""Permissions wiring for the sales boundary layer.

Proves the integration reuses the existing five-tier PermissionEngine rather than
inventing a new approval path: a sales worker's policy + a sales tool's declared
risk together decide auto/approve/deny, and the researcher worker cannot reach the
send/approve tools (separation of duties by tool allowlist).
"""

from __future__ import annotations

import os

from sworker.config import WorkerConfig, load_worker, DEFAULT_POLICY
from sworker.permissions import PermissionEngine, classify
from sworker.tools import build_registry
from sworker.sales.tools.base import SALES_TOOLS


def _build(name):
    return build_registry()


def test_sales_tools_declare_risk_tiers():
    reg = build_registry()
    seen = {}
    for t in SALES_TOOLS:
        seen[t.name] = t.risk.value
    # Drafting + sending are external (gated); discovery/research/qualify are reversible/read.
    assert seen["sales_draft_outreach"] in ("external", "reversible")
    assert seen["sales_record_sent"] == "external"
    assert seen["sales_approve_draft"] in ("external", "reversible")
    assert seen["sales_discover"] in ("read", "reversible")
    assert seen["sales_research"] in ("read", "reversible")
    assert seen["sales_qualify"] in ("read", "reversible")


def test_researcher_cannot_reach_send_tools():
    """Separation of duties: the researcher opt-in set excludes the egress tools."""
    researcher = load_worker(
        os.path.join(os.path.dirname(__file__), "..", "sworker", "sales", "templates", "sales_researcher.yaml")
    )
    allowed = set(researcher.tools)
    assert "sales_record_sent" not in allowed, "researcher must not send"
    assert "sales_approve_draft" not in allowed, "researcher must not approve"
    assert "sales_discover" in allowed


def test_outreach_send_requires_approval_under_default_policy():
    outreach = load_worker(
        os.path.join(os.path.dirname(__file__), "..", "sworker", "sales", "templates", "sales_outreach.yaml")
    )
    eng = PermissionEngine(outreach)
    reg = build_registry()
    send_tool = reg.get("sales_record_sent")
    decision = eng.evaluate(send_tool, {"draft_id": "d1", "receipt": "smtp:noop"})
    assert decision.needs_approval is True, decision
    assert decision.allowed is False, "external send must not be automatic"


def test_external_denied_when_policy_denies():
    cfg = WorkerConfig(
        name="strict",
        tools=[t.name for t in SALES_TOOLS],
        policy=dict(DEFAULT_POLICY, external="deny"),
    )
    eng = PermissionEngine(cfg)
    reg = build_registry()
    decision = eng.evaluate(reg.get("sales_record_sent"), {"draft_id": "d1"})
    assert decision.denied is True, decision
    assert decision.needs_approval is False


def test_read_tier_auto_under_default_policy():
    cfg = WorkerConfig(
        name="reader",
        tools=[t.name for t in SALES_TOOLS],
        policy=dict(DEFAULT_POLICY),
    )
    eng = PermissionEngine(cfg)
    reg = build_registry()
    decision = eng.evaluate(reg.get("sales_metrics"), {"day": ""})
    assert decision.allowed is True, decision
    assert decision.needs_approval is False
