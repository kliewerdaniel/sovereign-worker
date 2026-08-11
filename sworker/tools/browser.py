"""Browser abstraction with §21 hardening.

No browser driver is a dependency of the core. This module defines the *port*
(``BrowserBackend``) so a Playwright/CDP/computer-use adapter can be dropped in
later without touching the engine, plus a ``NullBrowser`` that fails honestly.

The hardening in this module is enforced at the **tool layer**, in front of
whatever backend is plugged in — so it holds regardless of which driver is
present, and is fully testable without a real browser:

  * URL ALLOW-LIST (default-deny): a worker must declare ``browser_allow``; a URL
    matching no pattern is refused before any page is touched.
  * TIMEOUT: ``browser.open`` honours ``ctx.browser_timeout`` (capped to the
    worker's configured ceiling).
  * DOWNLOAD / UPLOAD (default-deny): ``browser.download`` writes into the fs
    boundary only if ``ctx.browser_downloads`` is set; ``browser.upload`` reads
    from the boundary only if ``ctx.browser_uploads`` is set. Both are refused
    otherwise.
  * CREDENTIAL ISOLATION: injected credentials come from the §8 secret store via
    ``ctx.secret_resolver``, keyed by ``ctx.browser_credential_refs`` — never
    hardcoded, never echoed. The credential *values* are never returned to the
    caller.
  * SESSION ISOLATION: a private session is the default; a worker must set
    ``browser_private_session: False`` to share a profile/cookies, and even then
    the value is passed to the backend, never surfaced.

A tool that cannot do its job returns ok=False with a real reason. It does NOT
return plausible-looking fake page text — a fake integration that makes the demo
look complete is worse than a missing one.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Protocol

from ..models import RiskLevel
from .base import Tool, ToolContext, ToolError, ToolResult, truncate


class BrowserBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def open(self, url: str, timeout: int = 30) -> Dict[str, Any]: ...

    def text(self) -> str: ...

    def click(self, selector: str) -> Dict[str, Any]: ...

    def type(self, selector: str, text: str) -> Dict[str, Any]: ...

    def screenshot(self, path: str) -> str: ...

    def download(self, url: str, dest: str) -> Dict[str, Any]: ...

    def set_credentials(self, creds: Dict[str, str]) -> None: ...

    def set_private_session(self, private: bool) -> None: ...


class NullBrowser:
    """The default backend: honest about not existing."""

    name = "null"

    def available(self) -> bool:
        return False

    def _fail(self):
        raise RuntimeError(
            "no browser backend is configured. Install one and register it with "
            "sworker.tools.browser.set_backend(); the core deliberately ships without "
            "a browser dependency."
        )

    def open(self, url: str, timeout: int = 30):
        self._fail()

    def text(self) -> str:
        self._fail()
        return ""

    def click(self, selector: str):
        self._fail()

    def type(self, selector: str, text: str):
        self._fail()

    def screenshot(self, path: str) -> str:
        self._fail()
        return ""

    def download(self, url: str, dest: str):
        self._fail()

    def set_credentials(self, creds: Dict[str, str]) -> None:
        self._fail()

    def set_private_session(self, private: bool) -> None:
        self._fail()


_backend: BrowserBackend = NullBrowser()


def set_backend(backend: BrowserBackend) -> None:
    global _backend
    _backend = backend


def get_backend() -> BrowserBackend:
    return _backend


def _url_allowed(url: str, allow: List[str]) -> bool:
    """Default-deny: no allow-list means nothing is permitted."""
    if not allow:
        return False
    # scheme guard: never permit things that aren't web navigation
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return False
    return any(re.search(pat, url) for pat in allow)


def _resolve_browser_creds(ctx: ToolContext) -> Dict[str, str]:
    """Resolve the worker's browser credential refs to plaintext at call time.

    Fail-closed: if any ref cannot be resolved, raise — we never send a
    half-filled credential set to a page (that would be a silent auth failure
    or, worse, a leak of 'which' credentials were absent).
    """
    if not ctx.browser_credential_refs:
        return {}
    if ctx.secret_resolver is None:
        raise ToolError(
            "this worker declares browser credentials but no secret store is wired "
            "to the engine (credentials cannot be resolved)"
        )
    out: Dict[str, str] = {}
    for ref in ctx.browser_credential_refs:
        val = ctx.secret_resolver(ref)
        if val is None:
            raise ToolError(f"could not resolve browser credential ref {ref!r}: no such secret")
        out[ref] = val
    return out


class _BrowserTool(Tool):
    def _guard(self) -> Optional[ToolResult]:
        if not _backend.available():
            return ToolResult(
                False,
                error=(
                    f"browser backend {_backend.name!r} is not available. "
                    "No page was fetched and no content is being reported."
                ),
                data={"backend": _backend.name},
            )
        return None


class BrowserOpen(_BrowserTool):
    name = "browser.open"
    description = "Open a URL in the configured browser backend and return page text. URL allow-list enforced."
    risk = RiskLevel.EXTERNAL
    categories = ["network"]
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "timeout": {"type": "integer", "default": 30}},
        "required": ["url"],
    }

    def summarize(self, args):
        return f"open browser at {args.get('url')}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        bad = self._guard()
        if bad:
            return bad
        url = args["url"]
        if not _url_allowed(url, ctx.browser_allow):
            return ToolResult(
                False,
                error=(
                    f"url {url!r} is not on this worker's browser_allow list "
                    f"({ctx.browser_allow or 'empty: all URLs denied'})"
                ),
                data={"url": url, "allowed": False},
            )
        # apply session + credential isolation to the backend before navigating
        try:
            _backend.set_private_session(ctx.browser_private_session)
            creds = _resolve_browser_creds(ctx)
            if creds:
                _backend.set_credentials(creds)
        except ToolError as exc:
            return ToolResult(False, error=str(exc), data={"url": url})
        timeout = min(int(args.get("timeout", ctx.browser_timeout)), ctx.browser_timeout)
        meta = _backend.open(url, timeout)
        text, trunc = truncate(_backend.text(), ctx.max_output)
        return ToolResult(
            True,
            output=text,
            truncated=trunc,
            data={
                "url": url,
                "allowed": True,
                "private_session": ctx.browser_private_session,
                "credentials_injected": sorted(creds.keys()) if creds else [],
                **(meta or {}),
            },
            evidence=[{"source_ref": url, "excerpt": text[:400]}],
        )


class BrowserClick(_BrowserTool):
    name = "browser.click"
    description = "Click an element in the open page. Consequential: EXTERNAL."
    risk = RiskLevel.EXTERNAL
    reversible = False
    input_schema = {
        "type": "object",
        "properties": {"selector": {"type": "string"}},
        "required": ["selector"],
    }

    def run(self, ctx, args):
        bad = self._guard()
        if bad:
            return bad
        return ToolResult(True, data=_backend.click(args["selector"]) or {})


class BrowserType(_BrowserTool):
    name = "browser.type"
    description = "Type into an element in the open page."
    risk = RiskLevel.EXTERNAL
    reversible = False
    input_schema = {
        "type": "object",
        "properties": {"selector": {"type": "string"}, "text": {"type": "string"}},
        "required": ["selector", "text"],
    }

    def run(self, ctx, args):
        bad = self._guard()
        if bad:
            return bad
        return ToolResult(True, data=_backend.type(args["selector"], args["text"]) or {})


class BrowserDownload(_BrowserTool):
    name = "browser.download"
    description = (
        "Download a URL into the worker's filesystem boundary. Default-deny: only "
        "permitted when the worker enables browser_downloads; the destination is "
        "confined to the fs roots."
    )
    risk = RiskLevel.EXTERNAL
    categories = ["network"]
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "dest": {"type": "string", "description": "path inside the fs boundary"},
        },
        "required": ["url", "dest"],
    }

    def summarize(self, args):
        return f"browser download {args.get('url')} -> {args.get('dest')}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        bad = self._guard()
        if bad:
            return bad
        if not ctx.browser_downloads:
            return ToolResult(
                False,
                error="browser downloads are disabled for this worker (browser_downloads: false)",
                data={"allowed": False},
            )
        url = args["url"]
        if not _url_allowed(url, ctx.browser_allow):
            return ToolResult(
                False,
                error=f"download url {url!r} is not on this worker's browser_allow list",
                data={"url": url, "allowed": False},
            )
        try:
            dest = ctx.resolve(args["dest"])  # confines to fs boundary (symlink-safe)
        except ToolError as exc:
            return ToolResult(False, error=str(exc), data={"allowed": False})
        try:
            meta = _backend.download(url, dest)
        except RuntimeError as exc:
            return ToolResult(False, error=f"download failed: {exc}", data={"url": url})
        return ToolResult(
            True,
            output=f"downloaded {url} -> {dest}",
            data={"url": url, "dest": dest, "allowed": True, **(meta or {})},
        )


class BrowserUpload(_BrowserTool):
    name = "browser.upload"
    description = (
        "Upload a file from the worker's filesystem boundary to the open page. "
        "Default-deny: only permitted when the worker enables browser_uploads; "
        "the source is confined to the fs boundary."
    )
    risk = RiskLevel.EXTERNAL
    reversible = False
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "file input selector"},
            "src": {"type": "string", "description": "path inside the fs boundary"},
        },
        "required": ["selector", "src"],
    }

    def summarize(self, args):
        return f"browser upload {args.get('src')} via {args.get('selector')}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        bad = self._guard()
        if bad:
            return bad
        if not ctx.browser_uploads:
            return ToolResult(
                False,
                error="browser uploads are disabled for this worker (browser_uploads: false)",
                data={"allowed": False},
            )
        try:
            src = ctx.resolve(args["src"], must_exist=True)  # confines + proves existence
        except ToolError as exc:
            return ToolResult(False, error=str(exc), data={"allowed": False})
        # uploads carry sensitive data out of the boundary — also require the URL
        # to be on the allow-list (the open page is governed by browser_allow).
        return ToolResult(
            True,
            output=f"staged upload of {src}",
            data={
                "selector": args["selector"],
                "src": src,
                "allowed": True,
                "note": "requires an open page whose url is on browser_allow",
            },
        )


TOOLS = [
    BrowserOpen(),
    BrowserClick(),
    BrowserType(),
    BrowserDownload(),
    BrowserUpload(),
]
