"""§22 messaging policy — default-deny channel allow-list, rate limit, draft, receipt.

Drives the `message.send` tool directly with a stub backend so the policy layer
(channel allow-list, rate cap, draft/no-egress, structured receipt) is verified
without a real messaging service.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from sworker.tools.base import ToolContext, ToolResult
from sworker.tools.message import SendMessage, _channel_allowed, get_backend, set_backend


class _StubBackend:
    """Records every send; carries delivered flag so we can assert no-egress."""

    name = "stub"

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def send(self, channel: str, text: str, ctx: ToolContext, delivered: bool) -> Dict[str, Any]:
        rec = {
            "receipt_id": "rcpt_test",
            "ts": 1.0,
            "run_id": ctx.run_id,
            "worker": ctx.worker,
            "channel": channel,
            "text": text,
            "delivered": delivered,
        }
        self.calls.append(rec)
        return rec


@pytest.fixture
def stub():
    b = _StubBackend()
    prev = get_backend()
    set_backend(b)
    yield b
    set_backend(prev)


def _ctx(**kw) -> ToolContext:
    base = dict(
        worker="w",
        run_id="r1",
        workspace="/tmp",
        fs_roots=["/tmp"],
        artifacts_dir="/tmp/artifacts",
        message_allow=[],
        message_rate_limit=0,
        messages_sent=0,
    )
    base.update(kw)
    return ToolContext(**base)


def test_channel_allow_empty_denies_all():
    assert _channel_allowed("general", []) is False
    assert _channel_allowed("#general", ["^general$"]) is True
    assert _channel_allowed("secret", ["^general$"]) is False


def test_send_refused_without_allow_list(stub):
    res = SendMessage().run(_ctx(), {"channel": "general", "text": "hi"})
    assert res.ok is False
    assert "message_allow" in res.error
    assert stub.calls == []


def test_send_permitted_on_allow_list(stub):
    ctx = _ctx(message_allow=["^general$"])
    res = SendMessage().run(ctx, {"channel": "#general", "text": "hi"})
    assert res.ok is True
    assert res.data["delivered"] is True
    assert res.data["receipt_id"] == "rcpt_test"
    assert stub.calls == [{"receipt_id": "rcpt_test", "ts": 1.0, "run_id": "r1",
                           "worker": "w", "channel": "#general", "text": "hi", "delivered": True}]


def test_draft_composes_without_egress_and_without_approval_block(stub):
    # drafts must respect the channel allow-list too, but never deliver
    ctx = _ctx(message_allow=["^general$"])
    res = SendMessage().run(ctx, {"channel": "general", "text": "draft", "draft": True})
    assert res.ok is True
    assert res.data["delivered"] is False
    # backend still recorded it, but explicitly NOT delivered
    assert stub.calls[0]["delivered"] is False
    # drafts do not consume the rate budget
    assert ctx.messages_sent == 0


def test_rate_limit_blocks_after_cap(stub):
    ctx = _ctx(message_allow=[".*"], message_rate_limit=2, messages_sent=0)
    assert SendMessage().run(ctx, {"channel": "a", "text": "1"}).ok is True
    assert SendMessage().run(ctx, {"channel": "b", "text": "2"}).ok is True
    third = SendMessage().run(ctx, {"channel": "c", "text": "3"})
    assert third.ok is False
    assert third.data["rate_limited"] is True
    assert len(stub.calls) == 2  # only the first two actually delivered


def test_rate_limit_ignores_drafts(stub):
    ctx = _ctx(message_allow=[".*"], message_rate_limit=1, messages_sent=0)
    # a draft does not count toward the cap
    assert SendMessage().run(ctx, {"channel": "a", "text": "d", "draft": True}).ok is True
    assert ctx.messages_sent == 0
    assert SendMessage().run(ctx, {"channel": "b", "text": "real"}).ok is True
    assert ctx.messages_sent == 1
    # cap now reached
    assert SendMessage().run(ctx, {"channel": "c", "text": "real2"}).ok is False


def test_receipt_structured_and_auditable(stub):
    ctx = _ctx(message_allow=[".*"])
    res = SendMessage().run(ctx, {"channel": "general", "text": "hi"})
    assert set(["receipt_id", "channel", "delivered", "ts", "backend"]) <= set(res.data)
    assert res.data["channel"] == "general"
    assert res.data["backend"] == "stub"


def test_draft_still_denied_on_disallowed_channel(stub):
    ctx = _ctx(message_allow=["^general$"])
    res = SendMessage().run(ctx, {"channel": "secret", "text": "x", "draft": True})
    assert res.ok is False
    assert "message_allow" in res.error
    assert stub.calls == []
