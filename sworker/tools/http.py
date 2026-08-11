"""HTTP tool. Network egress is a boundary crossing, so it is EXTERNAL risk for
anything that is not a plain GET to an allowlisted host. See docs/SECURITY.md
(section 3) for the SSRF caveats and the `auth_env` / scheme rules.

§54 network egress registry: every outbound request is checked against the
worker's default-deny ``egress_allow`` host allow-list BEFORE any bytes leave the
machine. An empty allow-list denies all egress. The host is also checked for
classic SSRF targets (link-local / metadata / loopback-as-external) regardless of
the allow-list, so a typo'd pattern can't quietly open the cloud metadata
service. Both the decision and the actual destination are recorded on the
observation so the UI can show exactly what tried to leave and what was allowed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from ..models import RiskLevel
from .base import Tool, ToolContext, ToolError, ToolResult, truncate
from ..dlp import DlpPolicy

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Never let an advertised allow-list open these, even if a pattern matches.
SSRF_BLOCKED = {
    "169.254.169.254",         # cloud metadata
    "metadata.google.internal",
    "169.254.169.254.nip.io",
}
SSRF_SUBNETS = ("169.254.", "127.", "10.", "192.168.", "172.16.", "172.17.",
                "172.18.", "172.19.", "172.2", "172.30.", "172.31.")


def _host_allowed(host: str, allow: list) -> bool:
    """Default-deny: no allow-list means no egress."""
    if not allow:
        return False
    return any(re.search(pat, host) for pat in allow)


def _ssrf_blocked(host: str) -> Optional[str]:
    h = (host or "").lower().rstrip(".")
    if h in SSRF_BLOCKED:
        return f"blocked SSRF target {h!r} (metadata/link-local)"
    if any(h.startswith(s) for s in SSRF_SUBNETS):
        return f"blocked private/link-local host {h!r}"
    return None


def _check_egress(url: str, ctx: ToolContext) -> Optional[str]:
    """Return a refusal reason (or None if allowed) for an outbound URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme: {parsed.scheme!r}"
    host = parsed.hostname or ""
    blocked = _ssrf_blocked(host)
    if blocked:
        return blocked
    if not _host_allowed(host, ctx.egress_allow):
        return (
            f"host {host!r} is not on this worker's egress_allow list "
            f"({ctx.egress_allow or 'empty: all egress denied'})"
        )
    return None


def _request(method: str, url: str, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    refusal = _check_egress(url, ctx)
    if refusal:
        # Fail closed: refuse BEFORE any network contact. Record the reason so
        # the observation/UI can show what was attempted and why it was blocked.
        return ToolResult(
            False,
            error=refusal,
            data={"url": url, "egress": False, "refused": True, "reason": refusal},
        )
    # §55 DLP: scan the URL (for GET) and the body (for POST) for secrets/PII
    # BEFORE any bytes leave the machine. Fail closed — a match refuses egress
    # and records only the rule that fired, never the matched text.
    dlp = DlpPolicy(ctx.dlp_rules) if ctx.dlp_rules else None
    if dlp is not None:
        haystack = url
        if args.get("body") is not None:
            raw = args["body"]
            haystack = f"{haystack}\n{raw if isinstance(raw, str) else json.dumps(raw)}"
        hit = dlp.scan(haystack)
        if hit is not None:
            return ToolResult(
                False,
                error=DlpPolicy.refusal_for(hit),
                data={"url": url, "egress": False, "dlp_blocked": True,
                      "rule": hit.rule, "kind": hit.kind},
            )
    headers = dict(args.get("headers") or {})
    auth = args.get("auth_env")
    if auth:
        import os

        if auth not in ctx.env_allow:
            return ToolResult(
                False,
                error=(
                    f"auth_env {auth!r} is not in this worker's env_allow list; refusing to "
                    "read an undeclared credential"
                ),
                data={"url": url, "egress": True, "auth_refused": True},
            )
        token = os.environ.get(auth)
        if not token:
            return ToolResult(False, error=f"env var {auth} is not set",
                              data={"url": url, "egress": True})
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if args.get("body") is not None:
        raw = args["body"]
        body = (raw if isinstance(raw, str) else json.dumps(raw)).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=min(int(args.get("timeout", 30)), 120)) as r:
            text = r.read().decode("utf-8", errors="replace")
            status = r.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:
        return ToolResult(False, error=f"{type(exc).__name__}: {exc}",
                          data={"url": url, "egress": True})
    out, trunc = truncate(text, ctx.max_output)
    parsed_body = None
    try:
        parsed_body = json.loads(text)
    except Exception:
        pass
    ok = 200 <= status < 400
    return ToolResult(
        ok,
        output=f"HTTP {status}\n{out}",
        error="" if ok else f"HTTP {status}",
        truncated=trunc,
        data={"url": url, "status": status, "json": parsed_body, "egress": True},
        evidence=[{"source_ref": url, "excerpt": out[:400]}],
    )


class HttpGet(Tool):
    name = "http.get"
    description = (
        "HTTP GET. Subject to the worker's egress_allow host allow-list (default-deny); "
        "SSRF targets (metadata/link-local) are always blocked."
    )
    risk = RiskLevel.EXTERNAL
    categories = ["network"]
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "headers": {"type": "object", "default": {}},
            "auth_env": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["url"],
    }

    def summarize(self, args: Dict[str, Any]) -> str:
        return f"HTTP GET {args.get('url')}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        return _request("GET", args["url"], ctx, args)


class HttpPost(Tool):
    name = "http.post"
    description = (
        "HTTP POST with a JSON body. Subject to the worker's egress_allow host "
        "allow-list (default-deny); SSRF targets are always blocked."
    )
    risk = RiskLevel.EXTERNAL
    reversible = False
    categories = ["network"]
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "body": {"type": "object"},
            "headers": {"type": "object", "default": {}},
            "auth_env": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["url"],
    }

    def summarize(self, args: Dict[str, Any]) -> str:
        return f"HTTP POST {args.get('url')} (sends data off this machine)"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        return _request("POST", args["url"], ctx, args)


def risk_for_url(url: str) -> RiskLevel:
    """A GET against localhost is not an external action. Used by the engine to
    downgrade risk for local-only endpoints."""
    host = urllib.parse.urlparse(url).hostname or ""
    return RiskLevel.READ if host in LOCAL_HOSTS else RiskLevel.EXTERNAL


def render_egress_log(store) -> Dict[str, Any]:
    """§54 UI visibility: return every observation that touched the network
    boundary — what URL was attempted, whether it was allowed, and if refused,
    why. Sourced from stored observation records (no live contact)."""
    allowed, refused = [], []
    for obs in store.find("observations", order="created", desc=True):
        d = obs.get("data") or {}
        if "url" not in d:
            continue
        entry = {
            "run_id": obs.get("run_id"),
            "ok": obs.get("ok"),
            "url": d["url"],
            "status": d.get("status"),
            "egress": d.get("egress", False),
            "reason": d.get("reason"),
        }
        (refused if d.get("refused") else allowed).append(entry)
    return {"allowed": allowed, "refused": refused, "total": len(allowed) + len(refused)}


TOOLS = [HttpGet(), HttpPost()]

