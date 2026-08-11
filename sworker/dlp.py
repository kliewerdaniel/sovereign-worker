"""§55 DLP primitives — deterministic data-loss-prevention scanners.

This module provides secret/PII pattern detection that runs over egress
payloads (HTTP request bodies / URLs, message text) BEFORE they leave the
machine. It is fail-closed: if a payload matches a configured rule, the
egress is refused. The matched value is NEVER returned to the caller — only
the matched rule name + a human label, so an observation can say *what kind*
of secret was stopped without leaking the secret itself.

Design (zero-dep, fail-closed, operator-intent-required):
  * DLP is OPT-IN per worker via ``dlp_rules`` (a list of named rule ids).
    An empty rule list means no DLP scanning at all — we never silently scan
    with a built-in list, consistent with the other default-deny subsystems
    (egress_allow / message_allow / browser_allow).
  * The built-in catalog ``BUILTIN_DLP_RULES`` ships common detectors
    (AWS key, private-key block, API token, email, SSN, card number). A
    worker references them BY NAME — operator intent is required to turn a
    detector on. Unknown names fail closed (KeyError at policy build time).
  * A scanner returns the first hit. The observation records only
    ``rule`` + ``kind`` + ``dlp_blocked`` — never the matched text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class DlpRule:
    name: str          # referenced by workers in dlp_rules
    kind: str          # human label: "aws_access_key_id", "private_key_block", ...
    pattern: str       # regex (compiled lazily, cached on the instance)


@dataclass
class DlpHit:
    rule: str
    kind: str


# Built-in detector catalog. Operators turn these on by naming them in a
# worker's `dlp_rules`. Never applied unless explicitly referenced.
BUILTIN_DLP_RULES: Dict[str, DlpRule] = {
    "aws_access_key_id": DlpRule(
        "aws_access_key_id", "aws_access_key_id",
        r"(?<![A-Za-z0-9/+=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9/+=])",
    ),
    "private_key_block": DlpRule(
        "private_key_block", "private_key_block",
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
    ),
    "api_token": DlpRule(
        "api_token", "api_token",
        r"(?i)(?:api[_-]?key|secret|token|bearer|authorization)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?",
    ),
    "email_address": DlpRule(
        "email_address", "email_address",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    ),
    "us_ssn": DlpRule(
        "us_ssn", "us_ssn",
        r"\b\d{3}-\d{2}-\d{4}\b",
    ),
    "credit_card": DlpRule(
        "credit_card", "credit_card",
        r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13})\b",
    ),
}


class DlpPolicy:
    """Compiled, per-worker DLP policy built from a list of rule names."""

    def __init__(self, names: Optional[List[str]] = None):
        self.names = list(names or [])
        self.rules: List[DlpRule] = []
        for n in self.names:
            rule = BUILTIN_DLP_RULES.get(n)
            if rule is None:
                raise KeyError(
                    f"unknown dlp_rule {n!r}; available: {sorted(BUILTIN_DLP_RULES)}"
                )
            self.rules.append(rule)

    def scan(self, text: str) -> Optional[DlpHit]:
        """Return the first matching rule (or None). Never returns the text."""
        if not text:
            return None
        for rule in self.rules:
            if re.search(rule.pattern, text):
                return DlpHit(rule=rule.name, kind=rule.kind)
        return None

    @staticmethod
    def refusal_for(hit: DlpHit) -> str:
        return (
            f"DLP policy blocked egress: matched rule {hit.rule!r} "
            f"({hit.kind}); payload was not transmitted"
        )


def render_dlp_log(store) -> Dict[str, object]:
    """§55 UI visibility: return every observation that was blocked by DLP,
    sourced from stored observations (no live contact). The matched payload is
    never included — only which rule fired."""
    blocked = []
    for obs in store.find("observations", order="created", desc=True):
        d = obs.get("data") or {}
        if not d.get("dlp_blocked"):
            continue
        blocked.append({
            "run_id": obs.get("run_id"),
            "ok": obs.get("ok"),
            "url": d.get("url"),
            "channel": d.get("channel"),
            "rule": d.get("rule"),
            "kind": d.get("kind"),
        })
    return {"blocked": blocked, "total": len(blocked)}
