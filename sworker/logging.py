"""§35 structured logging + redaction.

Local-first, zero-dep. Emits JSON lines (one event per line) so logs are
machine-parseable and never silently drop context. ``redact`` masks high-signal
fields by default (fail closed): secrets, tokens, credentials, emails, and any
value whose key looks sensitive. Redaction is opt-OUT (``redact=False``), never
opt-IN, so a misconfiguration leaks nothing by default.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict

# keys whose *values* are masked wholesale
_SENSITIVE_KEYS = (
    "password", "token", "secret", "api_key", "apikey", "authorization",
    "credential", "credentials", "private_key", "access_key", "session",
    "cookie", "set_cookie",
)
# free-text patterns (emails, bearer tokens, long base64/hex secrets)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+[A-Za-z0-9._\-]+)")
_LONG_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_\-]{32,})\b")

MASK = "***REDACTED***"


def _key_is_sensitive(key: str) -> bool:
    k = key.lower().replace("-", "_").replace(" ", "_")
    return any(s in k for s in _SENSITIVE_KEYS)


def _redact(value: Any, *, redact: bool = True) -> Any:
    """§35 — recursively mask sensitive data. Default redact=True (fail closed)."""
    if not redact:
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if _key_is_sensitive(str(k)):
                out[k] = MASK
            else:
                out[k] = _redact(v, redact=True)
        return out
    if isinstance(value, list):
        return [_redact(v, redact=True) for v in value]
    if isinstance(value, str):
        v = _EMAIL_RE.sub(MASK, value)
        v = _BEARER_RE.sub(lambda m: f"Bearer {MASK}", v)
        v = _LONG_TOKEN_RE.sub(lambda m: MASK, v)
        return v
    return value


def redact(value: Any, *, redact: bool = True) -> Any:
    """§35 — public mask entry point (delegates to ``_redact``)."""
    return _redact(value, redact=redact)


def log_event(event: str, payload: Dict[str, Any], *, redact: bool = True) -> str:
    """§35 — emit one structured JSON log line. Returns the line so callers/tests
    can capture it instead of writing to stderr if they pass a sink."""
    record = {"event": event, **payload}
    record = _redact(record, redact=redact)
    return json.dumps(record, default=str, sort_keys=True)


class StructuredLogger:
    """§35 — minimal JSON-line logger. Writes to ``sink`` (default stderr)."""

    def __init__(self, sink=None, *, redact: bool = True) -> None:
        self.sink = sink if sink is not None else sys.stderr
        self.redact = redact

    def log(self, event: str, **payload: Any) -> None:
        line = log_event(event, payload, redact=self.redact)
        try:
            self.sink.write(line + "\n")
        except Exception:
            # never let logging crash the run
            pass

    def info(self, event: str, **payload: Any) -> None:
        self.log(event, level="info", **payload)

    def warn(self, event: str, **payload: Any) -> None:
        self.log(event, level="warn", **payload)

    def error(self, event: str, **payload: Any) -> None:
        self.log(event, level="error", **payload)
