"""§44 Prompt-injection defenses — deterministic content classifier.

The platform's primary injection defense is *architectural*: the model proposes
a plan once from the user request + worker config, and tool/data output flows
only into observations and artifacts — never back into the planner, never into
the permission decision, never into a new tool call. A retrieved file or web
page therefore cannot silently steer the run.

This module is the *explicit, testable* layer on top of that: a deterministic
scanner that inspects any content the worker ingests (file reads, knowledge
search hits, HTTP responses, messages) and flags instruction-like phrasing.
Flagged content is never trusted as instructions; the flag is recorded on the
observation so the audit trail shows exactly what was suspect, and the engine
refuses to let suspect content raise a run's risk ceiling or be treated as
worker guidance.

Design (zero-dep, fail-closed):
  * No model is used to judge "is this an injection" — that would be trusting
    the very surface we are defending. The classifier is a fixed set of
    regex/keyword patterns over the raw text.
  * Unknown/ambiguous content is treated as suspect (fail closed): if we cannot
    positively prove text is plain data, we do not let it masquerade as a
    command. The detector returns a structured verdict, never a silent pass.
  * The matched text is NEVER returned — only the rule that fired, so an
    observation can say *what kind* of injection was suspected without
    echoing attacker-controlled content into the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class InjectionVerdict:
    suspect: bool
    rule: Optional[str]  # which pattern fired, or None when benign
    kind: str            # human label for the fired rule (or "benign")

    def __bool__(self) -> bool:
        return self.suspect


# --- pattern catalog -------------------------------------------------------
# Each rule: (name, kind, regex). Ordered most-specific-first. The regexes are
# deliberately broad: an injection attempt is an attacker, so we bias toward
# flagging rather than risking a miss.
_INJECTION_RULES: List[tuple] = [
    (
        "ignore_previous_instructions",
        "instruction-override",
        r"(?i)(ignore|disregard|forget|override|disobey)\b.{0,40}\b(previous|prior|above|earlier|system|original)\b.{0,20}(instruction|prompt|directive|rule|guideline|policy)",
    ),
    (
        "system_prompt_leak_request",
        "system-prompt-extraction",
        r"(?i)(repeat|print|output|reveal|show|dump|leak|disclose)\b.{0,30}\b(system\s*prompt|your\s*instruction|initial\s*prompt|developer\s*mode|hidden\s*prompt)",
    ),
    (
        "roleplay_jailbreak",
        "role-play-jailbreak",
        r"(?i)(you\s*are\s*now|pretend\s*(to\s*be|you\s*are)|act\s*as|role\s*=\s*|from\s*now\s*on\s*you|new\s*persona|developer\s*mode|jailbreak|DAN\s*mode)",
    ),
    (
        "tool_or_action_command",
        "embedded-command",
        r"(?i)(run|execute|call|invoke|perform|send|exfiltrate|exfil)\b.{0,25}\b(tool|command|script|shell|curl|http|request|email|message|the\s*following)",
    ),
    (
        "secret_exfil_instruction",
        "secret-exfiltration",
        r"(?i)(send|post|upload|exfiltrate|forward|leak|transmit)\b.{0,30}\b(token|password|secret|api[_-]?key|credential|env\b|environment\b|private[_-]?key)",
    ),
    (
        "authority_forgery",
        "authority-forgery",
        r"(?i)(the\s*(admin|operator|developer|system|engine)\s*(says|commanded|instructed|requires|authorizes)|i\s*am\s*(the\s*)?(admin|operator|system|root)|as\s*(an?\s*)?(admin|operator|root|system)\b.{0,20}(you\s*must|do|run|allow))",
    ),
    (
        "delimiter_injection",
        "delimiter-injection",
        r"(?i)(<\|\s*system\s*\||<<\s*system\s*\|>|\[SYSTEM\]|\[INST\]|###\s*instruction|</?(system|assistant|user)>)",
    ),
]


_COMPILED = [(n, k, re.compile(p)) for (n, k, p) in _INJECTION_RULES]


# Keywords that, on their own in structured data, are expected (e.g. a column
# named "command" in a CSV). Used only to keep the verdict explainable; they do
# not downgrade a fired rule.
_BENIGN_HINTS = ("quarter", "revenue", "sales", "orders", "region", "total", "sum",
                 "count", "average", "price", "customer", "invoice", "product")


def scan(text: str) -> InjectionVerdict:
    """Return a deterministic verdict for ``text``.

    Fail-closed: if ``text`` is empty or non-string it is treated as benign
    (there is nothing to inject), but any non-empty content that matches a rule
    is flagged. The matched text is never returned.
    """
    if not text or not isinstance(text, str):
        return InjectionVerdict(False, None, "benign")
    for name, kind, rx in _COMPILED:
        if rx.search(text):
            return InjectionVerdict(True, name, kind)
    return InjectionVerdict(False, None, "benign")


def scan_dict(payload: dict) -> InjectionVerdict:
    """Scan a dict of ingested content (e.g. an observation's data + output).

    The most severe (first matching) verdict wins. Only string values are
    inspected; structured data cannot carry instructions.
    """
    worst: Optional[InjectionVerdict] = None
    for value in payload.values():
        if isinstance(value, str):
            v = scan(value)
            if v.suspect:
                # first fired rule wins; keep scanning only to preserve a hit
                if worst is None:
                    worst = v
        elif isinstance(value, dict):
            v = scan_dict(value)
            if v.suspect and worst is None:
                worst = v
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    v = scan(item)
                    if v.suspect and worst is None:
                        worst = v
                elif isinstance(item, dict):
                    v = scan_dict(item)
                    if v.suspect and worst is None:
                        worst = v
    return worst or InjectionVerdict(False, None, "benign")
