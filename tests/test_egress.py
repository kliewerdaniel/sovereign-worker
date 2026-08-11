"""§54 network egress registry — default-deny host allow-list + SSRF guard + UI
visibility (observation tagging + /api/egress log)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from sworker.store import WorkerStore
from sworker.models import Observation
from sworker.tools.base import ToolContext
from sworker.tools.http import (
    HttpGet,
    _check_egress,
    _host_allowed,
    _ssrf_blocked,
    render_egress_log,
)


def _ctx(**kw) -> ToolContext:
    base = dict(
        worker="w", run_id="r1", workspace="/tmp", fs_roots=["/tmp"],
        artifacts_dir="/tmp/art", shell_allow=[], env_allow=["TOKEN"],
        max_output=20000, egress_allow=[],
    )
    base.update(kw)
    return ToolContext(**base)


def test_host_allowed_default_deny_empty():
    assert _host_allowed("api.example.com", []) is False
    assert _host_allowed("api.example.com", ["^api\\.example\\.com$"]) is True
    assert _host_allowed("evil.example.com", ["^api\\.example\\.com$"]) is False


def test_ssrf_blocklist_always_blocks():
    assert _ssrf_blocked("169.254.169.254") is not None
    assert _ssrf_blocked("metadata.google.internal") is not None
    assert _ssrf_blocked("127.0.0.1") is not None
    assert _ssrf_blocked("10.0.0.5") is not None
    assert _ssrf_blocked("8.8.8.8") is None


def test_check_egress_refuses_without_allow_list():
    assert _check_egress("https://api.example.com/x", _ctx()) is not None
    assert "egress_allow" in _check_egress("https://api.example.com/x", _ctx())


def test_check_egress_allows_matching_host():
    ok = _check_egress("https://api.example.com/v1", _ctx(egress_allow=["^api\\.example\\.com$"]))
    assert ok is None


def test_check_egress_blocks_ssrf_even_if_pattern_matches():
    # A pattern that *would* match still can't open the metadata service.
    assert _check_egress("http://169.254.169.254/latest", _ctx(egress_allow=[".*"])) is not None


def test_check_egress_blocks_nonhttp_scheme():
    assert _check_egress("file:///etc/passwd", _ctx(egress_allow=[".*"])) is not None
    assert _check_egress("gopher://x", _ctx(egress_allow=[".*"])) is not None


def test_http_get_refused_records_reason(monkeypatch):
    """A refused request returns ok=False with the reason and never contacts net."""
    captured = {}

    def fake_urlopen(*a, **k):
        captured["called"] = True
        raise AssertionError("network contact should not happen on refusal")

    monkeypatch.setattr("sworker.tools.http.urllib.request.urlopen", fake_urlopen)
    res = HttpGet().run(_ctx(egress_allow=["^allowed\\.com$"]), {"url": "https://blocked.com/x"})
    assert res.ok is False
    assert res.data["refused"] is True
    assert "egress_allow" in res.data["reason"]
    assert captured == {}  # urlopen never invoked


def test_http_get_allowed_egresses(monkeypatch):
    monkeypatch.setattr(
        "sworker.tools.http.urllib.request.urlopen",
        lambda req, timeout=0: _FakeResp(b'{"ok":1}', 200),
    )
    res = HttpGet().run(_ctx(egress_allow=[".*"]), {"url": "https://api.example.com/v1"})
    assert res.ok is True
    assert res.data["egress"] is True
    assert res.data["status"] == 200


class _FakeResp:
    def __init__(self, body: bytes, status: int):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_egress_log_splits_allowed_and_refused(tmp_path):
    store = WorkerStore(str(tmp_path))
    store.put("observations", Observation(
        run_id="r1", action_id="a1", ok=False,
        data={"url": "https://blocked.com/x", "egress": False, "refused": True,
              "reason": "not on egress_allow"},
    ))
    store.put("observations", Observation(
        run_id="r1", action_id="a2", ok=True,
        data={"url": "https://api.example.com/v1", "egress": True, "status": 200},
    ))
    log = render_egress_log(store)
    assert log["total"] == 2
    assert len(log["allowed"]) == 1 and log["allowed"][0]["url"].endswith("/v1")
    assert len(log["refused"]) == 1 and "egress_allow" in (log["refused"][0]["reason"] or "")
