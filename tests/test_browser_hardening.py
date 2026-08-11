"""§21 browser hardening — default-deny enforcement of every axis.

These tests drive the browser *tools* directly with a stub backend so the
policy layer (URL allow-list, timeout cap, download/upload default-deny,
credential isolation, private session) is verified without a real browser
driver. The policy sits in front of whatever backend is plugged in, so it
holds regardless of driver.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from sworker.tools.browser import (
    BrowserOpen,
    BrowserDownload,
    BrowserUpload,
    _url_allowed,
    get_backend,
    set_backend,
)
from sworker.tools.base import ToolContext, ToolResult


class _StubBrowser:
    """Minimal backend that records what it was told to do."""

    name = "stub"

    def __init__(self):
        self._available = True
        self.opens: List[str] = []
        self.private = True
        self.creds: Dict[str, str] = {}
        self.downloads: List[tuple] = []

    def available(self) -> bool:
        return self._available

    def open(self, url: str, timeout: int = 30) -> Dict[str, Any]:
        self.opens.append((url, timeout))
        return {"title": "stub page"}

    def text(self) -> str:
        return "stub page body"

    def click(self, selector: str):
        return {"clicked": selector}

    def type(self, selector: str, text: str):
        return {"typed": selector}

    def screenshot(self, path: str) -> str:
        return path

    def download(self, url: str, dest: str) -> Dict[str, Any]:
        self.downloads.append((url, dest))
        return {"bytes": 10}

    def set_credentials(self, creds: Dict[str, str]) -> None:
        self.creds = dict(creds)

    def set_private_session(self, private: bool) -> None:
        self.private = private


@pytest.fixture
def stub():
    b = _StubBrowser()
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
        browser_allow=[],
        browser_timeout=30,
        browser_downloads=False,
        browser_uploads=False,
        browser_credential_refs=[],
        browser_private_session=True,
        secret_resolver=None,
    )
    base.update(kw)
    return ToolContext(**base)


def test_url_allow_empty_denies_all():
    assert _url_allowed("https://acme.com", []) is False
    assert _url_allowed("https://acme.com", ["^https://acme\\.com"]) is True
    assert _url_allowed("https://evil.com", ["^https://acme\\.com"]) is False


def test_non_http_url_refused():
    # never permit non-web navigation (file://, javascript:, etc.)
    assert _url_allowed("file:///etc/passwd", [".*"]) is False
    assert _url_allowed("javascript:alert(1)", [".*"]) is False


def test_open_refused_without_allow_list(stub):
    res = BrowserOpen().run(_ctx(), {"url": "https://acme.com"})
    assert res.ok is False
    assert "browser_allow" in res.error
    assert stub.opens == []  # backend never touched


def test_open_permitted_on_allow_list(stub):
    ctx = _ctx(browser_allow=["^https://acme\\.com"])
    res = BrowserOpen().run(ctx, {"url": "https://acme.com/login"})
    assert res.ok is True
    assert res.data["allowed"] is True
    assert stub.opens == [("https://acme.com/login", 30)]


def test_open_timeout_capped_to_worker_ceiling(stub):
    # caller asks for 999s; engine/ctx cap is 5 -> backend must see 5
    ctx = _ctx(browser_allow=[".*"], browser_timeout=5)
    res = BrowserOpen().run(ctx, {"url": "https://acme.com", "timeout": 999})
    assert res.ok is True
    assert stub.opens[0][1] == 5


def test_download_denied_by_default(stub):
    ctx = _ctx(browser_allow=[".*"])
    res = BrowserDownload().run(ctx, {"url": "https://acme.com/a.txt", "dest": "a.txt"})
    assert res.ok is False
    assert "disabled" in res.error
    assert stub.downloads == []


def test_download_permitted_confined_to_fs(stub, tmp_path):
    ctx = _ctx(
        browser_allow=[".*"],
        browser_downloads=True,
        workspace=str(tmp_path),
        fs_roots=[str(tmp_path)],
    )
    dest = tmp_path / "out.txt"
    res = BrowserDownload().run(ctx, {"url": "https://acme.com/a.txt", "dest": "out.txt"})
    assert res.ok is True
    assert stub.downloads == [("https://acme.com/a.txt", str(dest))]


def test_upload_denied_by_default(stub):
    ctx = _ctx(browser_allow=[".*"])
    res = BrowserUpload().run(ctx, {"selector": "#file", "src": "x.txt"})
    assert res.ok is False
    assert "disabled" in res.error


def test_upload_permitted_but_confined_to_fs(stub, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    ctx = _ctx(
        browser_allow=[".*"],
        browser_uploads=True,
        workspace=str(tmp_path),
        fs_roots=[str(tmp_path)],
    )
    res = BrowserUpload().run(ctx, {"selector": "#file", "src": "x.txt"})
    assert res.ok is True
    assert res.data["src"] == str(f)


def test_upload_rejects_path_outside_boundary(stub, tmp_path):
    ctx = _ctx(
        browser_allow=[".*"],
        browser_uploads=True,
        workspace=str(tmp_path),
        fs_roots=[str(tmp_path)],
    )
    res = BrowserUpload().run(ctx, {"selector": "#file", "src": "/etc/passwd"})
    assert res.ok is False  # fs boundary enforced even when uploads enabled


def test_credentials_injected_but_never_returned(stub):
    def resolver(ref):
        return {"acme_user": "SECRET_USER_VALUE", "acme_pass": "SECRET_PASS_VALUE"}[ref]

    ctx = _ctx(
        browser_allow=[".*"],
        browser_credential_refs=["acme_user", "acme_pass"],
        secret_resolver=resolver,
    )
    res = BrowserOpen().run(ctx, {"url": "https://acme.com"})
    assert res.ok is True
    assert stub.creds == {"acme_user": "SECRET_USER_VALUE", "acme_pass": "SECRET_PASS_VALUE"}
    # the secret VALUES are never echoed back to the caller (only ref names are)
    assert "SECRET_USER_VALUE" not in str(res.data)
    assert "SECRET_PASS_VALUE" not in str(res.data)
    assert res.data["credentials_injected"] == ["acme_pass", "acme_user"]


def test_missing_credential_refuses(stub):
    def resolver(ref):
        return None  # secret absent

    ctx = _ctx(
        browser_allow=[".*"],
        browser_credential_refs=["missing"],
        secret_resolver=resolver,
    )
    res = BrowserOpen().run(ctx, {"url": "https://acme.com"})
    assert res.ok is False
    assert "resolve" in res.error


def test_creds_without_resolver_refuses(stub):
    ctx = _ctx(browser_allow=[".*"], browser_credential_refs=["acme_user"])
    res = BrowserOpen().run(ctx, {"url": "https://acme.com"})
    assert res.ok is False
    assert "secret store" in res.error


def test_private_session_isolated_by_default(stub):
    ctx = _ctx(browser_allow=[".*"])  # browser_private_session defaults True
    res = BrowserOpen().run(ctx, {"url": "https://acme.com"})
    assert res.ok is True
    assert stub.private is True
    assert res.data["private_session"] is True
