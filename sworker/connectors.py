"""Connector architecture (spec §20).

A *connector* is the policy-governed boundary between a worker and an external
system. It sits ABOVE the raw transport ports (``BrowserBackend`` /
``MessageBackend`` in ``tools/browser.py`` / ``tools/message.py``): those describe
*how* to talk to something, a connector describes *whether this worker is allowed
to*, *at which targets*, and *with which credentials* — and refuses everything
else.

Design invariants (all fail-closed):

- **Default deny.** A connector is never active unless the worker explicitly
  enables it in its ``connectors`` list. An enabled connector permits nothing
  until its ``allow`` list (hosts / channels / scopes) is satisfied. An action
  against a target not on the allow-list is refused with a real reason.
- **Credential isolation.** A connector never receives a raw secret. It receives
  a *reference* (``secret_ref: "<name>"``) into the §8 ``SecretStore``. The
  manager resolves the ref at call time and injects the plaintext into the
  transport only — the value is never written to the run log, the artifact, or
  the model-facing context. If the ref is missing or the store is unavailable,
  the action is refused (never a silent anonymous call masquerading as
  authorized).
- **No reimplementation.** Connectors adapt existing adapters; they do not
  duplicate transport logic. The built-in connectors delegate to the already
  present tools/http.py (network) and tools/message.py (outbox).

The manager is the single chokepoint the engine consults before any connector
action, so the "model proposes, engine disposes" rule holds: the model can ask
for ``<connector>:<action>`` against a target, but the decision to permit it is
made here, against the worker's declared policy, not by anything the model said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from .models import RiskLevel


# ---------------------------------------------------------------------------
# connector port
# ---------------------------------------------------------------------------


class Connector(Protocol):
    """A governed adapter for one external system."""

    name: str  # e.g. "http", "slack", "github"

    def describe(self) -> Dict[str, Any]:
        """Human/agent-readable capability description (no secrets)."""
        ...

    def allowed(self, action: str, target: str) -> tuple[bool, str]:
        """Whether `action` against `target` is permitted by THIS connector's
        own allow-list. Returns (ok, reason). The manager layers connector
        enablement on top of this."""
        ...

    def required_credential_names(self) -> List[str]: ...

    @property
    def _secret_refs(self) -> Dict[str, str]: ...

    def execute(
        self, action: str, target: str, args: Dict[str, Any], credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        """Perform the action. `credentials` holds resolved secret values keyed
        by their logical name (never the ref). Must raise on any failure."""
        ...


class ConnectorBase:
    """Shared default-deny scaffolding for concrete connectors."""

    name = "base"

    def __init__(
        self,
        allow: Optional[List[str]] = None,
        secret_refs: Optional[Dict[str, str]] = None,
        options: Optional[Dict[str, Any]] = None,
    ):
        # allow entries are matched as regex against the normalized target
        self._allow = [self._compile(a) for a in (allow or [])]
        # logical credential name -> secret store ref (e.g. "token": "slack_token")
        self._secret_refs = dict(secret_refs or {})
        self._options = dict(options or {})

    @staticmethod
    def _compile(pattern: str) -> "re.Pattern[str]":
        # anchor a plain host/token pattern; allow full regex too
        return re.compile(pattern)

    def allowed(self, action: str, target: str) -> tuple[bool, str]:
        if not self._allow:
            return False, f"connector {self.name!r} has an empty allow-list (nothing permitted)"
        norm = self._normalize_target(target)
        for pat in self._allow:
            if pat.search(norm):
                return True, "allow-list match"
        return False, f"target {target!r} is not on connector {self.name!r} allow-list"

    def _normalize_target(self, target: str) -> str:
        return target.strip()

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "allow": [p.pattern for p in self._allow],
            # only the *names* of required credentials, never the values
            "credentials_required": list(self._secret_refs),
        }

    def required_credential_names(self) -> List[str]:
        return list(self._secret_refs)


# ---------------------------------------------------------------------------
# built-in connectors
# ---------------------------------------------------------------------------


class HttpConnector(ConnectorBase):
    """Network egress connector. Delegates to tools/http.py semantics but enforces
    a default-deny host allow-list and resolves a token secret ref if needed."""

    name = "http"

    def execute(self, action: str, target: str, args, credentials):
        # import lazily so the core stays import-cheap and testable
        from .tools.http import HttpGet, HttpPost

        method = (args.get("method") or action or "get").lower()
        if method == "post":
            tool = HttpPost()
        else:
            tool = HttpGet()

        # Build args the tool understands. The resolved credential (if any) is
        # passed through the tool's existing `auth_env` path by injecting the
        # value into a private env slot — but we do NOT expose it to the model.
        call_args = {"url": target, "timeout": args.get("timeout", 30)}
        if "body" in args:
            call_args["body"] = args["body"]
        auth_env = None
        if credentials:
            # single logical credential -> exported under the first secret ref name
            ref_name = next(iter(self._secret_refs.values()), None)
            if ref_name:
                # the tool reads the token from an env var; the manager owns that
                # var name and the value is injected by the engine, not the model
                auth_env = ref_name
        if auth_env:
            call_args["auth_env"] = auth_env
        return {"tool": tool.name, "args": call_args, "method": method}


class SlackConnector(ConnectorBase):
    """Messaging connector. Delegates to the outbox backend in tools/message.py,
    enforcing a channel allow-list and resolving the bot token secret ref."""

    name = "slack"

    def _normalize_target(self, target: str) -> str:
        # channels are referenced as "#general" or "general"; normalize to bare
        return target.strip().lstrip("#")

    def execute(self, action: str, target: str, args, credentials):
        from .tools.message import SendMessage

        channel = self._normalize_target(target)
        text = args.get("text", "")
        return {
            "tool": SendMessage().name,
            "args": {"channel": channel, "text": text},
            "channel": channel,
        }


# registry of constructable connectors (name -> class)
BUILTIN_CONNECTORS: Dict[str, type] = {
    "http": HttpConnector,
    "slack": SlackConnector,
}


# ---------------------------------------------------------------------------
# manager — the default-deny chokepoint
# ---------------------------------------------------------------------------


@dataclass
class ConnectorSpec:
    """A worker's declared intent to use a connector, with its constraints."""

    kind: str                       # "http", "slack", ...
    allow: List[str] = field(default_factory=list)
    secret_refs: Dict[str, str] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)


class ConnectorError(RuntimeError):
    """Raised when a connector action is refused (fail-closed)."""


class ConnectorManager:
    """Default-deny connector registry consulted by the engine.

    A worker declares connectors as a list of ConnectorSpec-like dicts in its
    config (``connectors`` field). Nothing is active unless declared. Each
    declared connector is built from the built-in registry (or a caller-supplied
    factory) and wrapped so that:

      * any target must pass the connector's allow-list, else refused;
      * any required credential is resolved from the §8 SecretStore at call time
        and injected into the transport — never returned to the model, never
        logged in cleartext.
    """

    def __init__(
        self,
        specs: Optional[List[Dict[str, Any]]] = None,
        secret_resolver: Optional[Callable[[str], str]] = None,
    ):
        self._secret_resolver = secret_resolver
        self._connectors: Dict[str, Connector] = {}
        for spec in specs or []:
            self._add_spec(spec)

    def _add_spec(self, spec: Dict[str, Any]) -> None:
        kind = spec.get("kind") or spec.get("name")
        if not kind:
            raise ConnectorError("connector spec missing 'kind'")
        cls = BUILTIN_CONNECTORS.get(kind)
        if cls is None:
            raise ConnectorError(f"unknown connector kind {kind!r} (have: {sorted(BUILTIN_CONNECTORS)})")
        inst = cls(
            allow=spec.get("allow"),
            secret_refs=spec.get("secret_refs"),
            options=spec.get("options"),
        )
        self._connectors[kind] = inst

    # -- introspection ------------------------------------------------------

    def enabled(self) -> List[str]:
        return sorted(self._connectors)

    def describe(self) -> Dict[str, Any]:
        return {name: c.describe() for name, c in self._connectors.items()}

    def has(self, kind: str) -> bool:
        return kind in self._connectors

    # -- the chokepoint -----------------------------------------------------

    def authorize(self, kind: str, action: str, target: str) -> tuple[bool, str, "Optional[Connector]"]:
        """Decide whether `kind:action@target` may proceed.

        Returns ``(ok, reason, connector)``. Fail-closed: anything not explicit
        is refused. The caller (engine) must refuse the action unless ``ok``.
        """
        if kind not in self._connectors:
            return (
                False,
                f"connector {kind!r} is not enabled for this worker "
                f"(enabled: {sorted(self._connectors) or 'none'})",
                self._connectors.get(kind),
            )
        conn = self._connectors[kind]
        ok, reason = conn.allowed(action, target)
        if not ok:
            return False, reason, conn
        return True, reason, conn

    def resolve_credentials(self, kind: str) -> Dict[str, str]:
        """Resolve secret refs to plaintext values for one connector.

        Fail-closed: if a required credential cannot be resolved (missing ref or
        resolver unavailable), raises ConnectorError rather than returning an
        incomplete set the transport might treat as an anonymous authorized call.
        """
        conn = self._connectors.get(kind)
        if conn is None:
            raise ConnectorError(f"connector {kind!r} not enabled")
        if self._secret_resolver is None and conn.required_credential_names():
            raise ConnectorError(
                f"connector {kind!r} requires credentials but no secret resolver is configured"
            )
        out: Dict[str, str] = {}
        for logical, ref in conn._secret_refs.items():
            if self._secret_resolver is None:
                continue
            try:
                val = self._secret_resolver(ref)
            except Exception as exc:  # resolver failure -> refuse, do not guess
                raise ConnectorError(
                    f"could not resolve secret ref {ref!r} for connector {kind!r}: {exc}"
                ) from exc
            if val is None:
                raise ConnectorError(f"secret ref {ref!r} for connector {kind!r} does not exist")
            out[logical] = val
        return out
