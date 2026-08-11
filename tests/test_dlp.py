"""§55 DLP primitives — fail-closed secret/PII scanning at the egress boundary.

Verifies:
  * DlpPolicy scans opt-in rule names; unknown names fail closed (KeyError).
  * A matching payload is refused on http.get / http.post / message.send, with
    the matched text NEVER present in the result (only rule + kind).
  * No dlp_rules => no scanning (payloads pass; opt-in, never silent-scan).
  * render_dlp_log surfaces blocked observations without the payload.
"""

from typing import Any, Dict, List

from sworker.store import WorkerStore
from sworker.models import Observation
from sworker.tools.base import ToolContext
from sworker.tools.http import HttpGet, HttpPost
from sworker.tools.message import SendMessage, get_backend, set_backend
from sworker.dlp import DlpPolicy, BUILTIN_DLP_RULES, render_dlp_log
import pytest


def _ctx(**kw) -> ToolContext:
    base: Dict[str, Any] = dict(
        worker="w", run_id="r1", workspace="/tmp", fs_roots=["/tmp"],
        artifacts_dir="/tmp/artifacts", egress_allow=[".*"], dlp_rules=[],
        message_allow=[".*"],
    )
    base.update(kw)
    return ToolContext(**base)


class _StubBackend:
    name = "stub"

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def send(self, channel, text, ctx, delivered) -> Dict[str, Any]:
        rec = {"receipt_id": "rcpt_test", "ts": 1.0, "backend": "stub",
               "channel": channel, "text": text, "delivered": delivered}
        self.calls.append(rec)
        return rec


@pytest.fixture
def stub():
    b = _StubBackend()
    prev = get_backend()
    set_backend(b)
    yield b
    set_backend(prev)


# --- policy compilation -----------------------------------------------------

def test_unknown_rule_fails_closed():
    with pytest.raises(KeyError):
        DlpPolicy(["not_a_real_rule"])


def test_known_rule_compiles():
    pol = DlpPolicy(["aws_access_key_id"])
    assert pol.scan("AKIAIOSFODNN7EXAMPLE") is not None
    assert pol.scan("hello world") is None


def test_empty_rules_means_no_scan():
    assert DlpPolicy([]).scan("AKIAIOSFODNN7EXAMPLE") is None


# --- http.post body scanning ------------------------------------------------

def test_post_body_with_aws_key_blocked():
    ctx = _ctx(dlp_rules=["aws_access_key_id"])
    res = HttpPost().run(ctx, {"url": "https://api.example.com/x",
                               "body": {"token": "AKIAIOSFODNN7EXAMPLE"}})
    assert res.ok is False
    assert res.data.get("dlp_blocked") is True
    assert res.data.get("rule") == "aws_access_key_id"
    # never returns the secret
    assert "AKIA" not in res.error


def test_post_clean_body_passes():
    ctx = _ctx(dlp_rules=["aws_access_key_id"])
    res = HttpPost().run(ctx, {"url": "https://api.example.com/x",
                               "body": {"note": "all good"}})
    # refused only because there is no network in test, but NOT a dlp_block
    assert res.data.get("dlp_blocked") is not True


def test_no_dlp_rules_does_not_scan():
    ctx = _ctx(dlp_rules=[])
    res = HttpPost().run(ctx, {"url": "https://api.example.com/x",
                               "body": {"token": "AKIAIOSFODNN7EXAMPLE"}})
    assert res.data.get("dlp_blocked") is not True


# --- http.get url scanning --------------------------------------------------

def test_get_url_with_ssn_blocked():
    ctx = _ctx(dlp_rules=["us_ssn"], egress_allow=[".*"])
    res = HttpGet().run(ctx, {"url": "https://api.example.com/lookup?ssn=123-45-6789"})
    assert res.ok is False
    assert res.data.get("dlp_blocked") is True
    assert res.data.get("kind") == "us_ssn"


# --- message.send text scanning ---------------------------------------------

def test_message_with_private_key_blocked(stub):
    ctx = _ctx(dlp_rules=["private_key_block"])
    res = SendMessage().run(ctx, {"channel": "general",
                                  "text": "here is my key:\n-----BEGIN RSA PRIVATE KEY-----\n..."})
    assert res.ok is False
    assert res.data.get("dlp_blocked") is True
    assert res.data.get("rule") == "private_key_block"
    assert stub.calls == []  # never reached the backend


def test_message_clean_text_passes(stub):
    ctx = _ctx(dlp_rules=["private_key_block"])
    res = SendMessage().run(ctx, {"channel": "general", "text": "weekly update, all clear"})
    assert res.ok is True
    assert stub.calls


# --- UI visibility ----------------------------------------------------------

def test_render_dlp_log_splits_blocked(tmp_path):
    store = WorkerStore(str(tmp_path))
    store.put("observations", Observation(
        run_id="r1", action_id="a1", ok=False,
        data={"url": "https://x/y", "dlp_blocked": True,
              "rule": "aws_access_key_id", "kind": "aws_access_key_id"},
    ))
    store.put("observations", Observation(
        run_id="r2", action_id="a2", ok=True,
        data={"url": "https://x/z"},
    ))
    out = render_dlp_log(store)
    assert out["total"] == 1
    assert out["blocked"][0]["rule"] == "aws_access_key_id"
    # payload must never be persisted in the dlp log
    assert "AKIA" not in str(out)


def test_builtin_catalog_present():
    for needed in ["aws_access_key_id", "private_key_block", "api_token",
                   "email_address", "us_ssn", "credit_card"]:
        assert needed in BUILTIN_DLP_RULES
