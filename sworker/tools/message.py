"""Messaging abstraction with §22 hardening.

Slack is NOT a dependency — it is one possible adapter. The default backend
writes the message to an outbox file. That is not a fake integration pretending
to be Slack: the tool reports exactly what it did ("queued to outbox" / "drafted"),
and the artifact is a real file you can inspect. When a real Slack backend is
registered via ``set_backend`` it receives the same (already policy-checked)
call.

§22 hardening, enforced at the tool layer in front of any backend:
  * CHANNEL ALLOW-LIST (default-deny): a worker must declare ``message_allow``;
    a channel matching no pattern is refused before anything is written.
  * RATE LIMIT: if ``message_rate_limit > 0``, the tool refuses once the run's
    sent count reaches the cap (tracked on the shared ``ToolContext``).
  * DRAFT MODE: ``message.send`` with ``draft=true`` writes the message to the
    outbox as ``delivered: False`` and reports "drafted (not delivered)" — no
    egress, no approval needed to *compose*. Delivery still requires approval.
  * RECEIPT: every send/draft returns a structured receipt (``receipt_id``,
    ``channel``, ``delivered``, ``ts``, ``backend``) so a result is auditable.

A tool that cannot do its job returns ok=False with a real reason.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional, Protocol

from ..models import RiskLevel
from .base import Tool, ToolContext, ToolError, ToolResult
from ..dlp import DlpPolicy


def _channel_allowed(channel: str, allow: list) -> bool:
    """Default-deny: no allow-list means no channel is permitted."""
    if not allow:
        return False
    norm = channel.strip().lstrip("#")
    return any(re.search(pat, norm) for pat in allow)


class MessageBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def send(self, channel: str, text: str, ctx: ToolContext, delivered: bool) -> Dict[str, Any]: ...


class OutboxBackend:
    """Writes to <artifacts_dir>/outbox.jsonl. Local, inspectable, honest."""

    name = "outbox"

    def available(self) -> bool:
        return True

    def send(self, channel: str, text: str, ctx: ToolContext, delivered: bool) -> Dict[str, Any]:
        path = os.path.join(ctx.artifacts_dir, "outbox.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        receipt_id = f"rcpt_{int(time.time() * 1_000_000)}_{abs(hash((ctx.run_id, channel, text))) % 10_000}"
        rec = {
            "receipt_id": receipt_id,
            "ts": time.time(),
            "run_id": ctx.run_id,
            "worker": ctx.worker,
            "channel": channel,
            "text": text,
            "delivered": delivered,
            "note": "queued locally; no external service was contacted"
            if delivered else "drafted locally; not delivered",
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec


_backend: MessageBackend = OutboxBackend()


def set_backend(backend: MessageBackend) -> None:
    global _backend
    _backend = backend


def get_backend() -> MessageBackend:
    return _backend


class SendMessage(Tool):
    name = "message.send"
    description = (
        "Send (or draft) a message to a channel via the configured messaging backend. "
        "Channel allow-list enforced (default-deny). Delivery requires approval; "
        "draft=true composes without delivering."
    )
    risk = RiskLevel.EXTERNAL
    reversible = False
    requires_approval = True
    input_schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "text": {"type": "string"},
            "draft": {"type": "boolean", "default": False,
                      "description": "if true, compose to outbox without delivering (no approval needed to draft)"},
        },
        "required": ["channel", "text"],
    }

    def summarize(self, args: Dict[str, Any]) -> str:
        preview = (args.get("text") or "")[:120].replace("\n", " ")
        mode = "draft" if args.get("draft") else "send"
        return f"{mode} message to {args.get('channel')!r} via {_backend.name}: {preview!r}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        channel = args["channel"]
        draft = bool(args.get("draft", False))

        # 1) channel allow-list (default-deny), applies to both draft and send
        if not _channel_allowed(channel, ctx.message_allow):
            return ToolResult(
                False,
                error=(
                    f"channel {channel!r} is not on this worker's message_allow list "
                    f"({ctx.message_allow or 'empty: all channels denied'})"
                ),
                data={"channel": channel, "allowed": False},
            )

        # 1b) §55 DLP — scan message text for secrets/PII before it leaves
        # (drafts included: a drafted secret is still a leak waiting to send).
        # Fail closed; record only the rule that fired, never the matched text.
        dlp = DlpPolicy(ctx.dlp_rules) if ctx.dlp_rules else None
        if dlp is not None:
            hit = dlp.scan(args["text"])
            if hit is not None:
                return ToolResult(
                    False,
                    error=DlpPolicy.refusal_for(hit),
                    data={"channel": channel, "allowed": True, "dlp_blocked": True,
                          "rule": hit.rule, "kind": hit.kind},
                )

        # 2) rate limit — only counts *delivered* messages, not drafts
        if not draft and ctx.message_rate_limit > 0 and ctx.messages_sent >= ctx.message_rate_limit:
            return ToolResult(
                False,
                error=(
                    f"message rate limit reached ({ctx.message_rate_limit} per run); "
                    f"refusing to deliver further messages this run"
                ),
                data={"rate_limited": True, "sent": ctx.messages_sent, "limit": ctx.message_rate_limit},
            )

        if not _backend.available():
            return ToolResult(False, error=f"message backend {_backend.name!r} unavailable")

        # drafts never egress and never consume the rate budget
        delivered = not draft
        info = _backend.send(channel, args["text"], ctx, delivered=delivered)
        if delivered:
            ctx.messages_sent += 1

        verb = "drafted (not delivered)" if draft else "queued for delivery"
        return ToolResult(
            True,
            output=f"message {verb} to {channel} via {_backend.name}",
            data={
                "channel": channel,
                "allowed": True,
                "delivered": delivered,
                "receipt_id": info["receipt_id"],
                "ts": info["ts"],
                "backend": _backend.name,
                "messages_sent_this_run": ctx.messages_sent,
            },
        )


TOOLS = [SendMessage()]
