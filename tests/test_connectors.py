"""§20 connector architecture — default-deny, allow-list, credential isolation.

The connector manager is the chokepoint the engine consults before any worker
reaches an external system. Every test here enforces the fail-closed invariant:
nothing external is reachable unless explicitly enabled, allow-listed, and (when
credentials are required) resolvable from the §8 secret store.
"""

from __future__ import annotations

import pytest

from sworker.connectors import ConnectorManager, ConnectorError
from sworker.config import WorkerConfig
from sworker.store import WorkerStore
from sworker.engine import WorkerEngine
from sworker.inference import NullInference


def test_default_deny_nothing_enabled_when_no_specs():
    m = ConnectorManager([])
    assert m.enabled() == []
    ok, reason, conn = m.authorize("http", "get", "https://anything.example.com")
    assert ok is False
    assert "not enabled" in reason


def test_empty_allow_list_refuses_everything():
    m = ConnectorManager([{"kind": "http", "allow": [], "secret_refs": {}}])
    assert m.enabled() == ["http"]
    ok, reason, conn = m.authorize("http", "get", "https://x.example.com")
    assert ok is False
    assert "empty allow-list" in reason


def test_allow_list_permits_match_refuses_others():
    m = ConnectorManager(
        [{"kind": "http", "allow": [r"https://api\.acme\.com"], "secret_refs": {}}]
    )
    ok, _, _ = m.authorize("http", "get", "https://api.acme.com/orders")
    assert ok is True
    ok, reason, _ = m.authorize("http", "get", "https://evil.example.com")
    assert ok is False
    assert "allow-list" in reason


def test_unknown_connector_kind_rejected_at_build():
    with pytest.raises(ConnectorError):
        ConnectorManager([{"kind": "telegram", "allow": [".*"]}])


def test_slack_channel_allow_list():
    m = ConnectorManager(
        [{"kind": "slack", "allow": [r"^general$"], "secret_refs": {"token": "slack_token"}}]
    )
    ok, _, _ = m.authorize("slack", "send", "#general")
    assert ok is True
    ok, reason, _ = m.authorize("slack", "send", "#secret-room")
    assert ok is False
    assert "allow-list" in reason


def test_credential_resolution_injects_values_not_refs():
    """A connector receives resolved plaintext keyed by logical name — never
    the ref, never the value echoed back to callers."""
    store = {"slack_token": "plaintext-secret-value"}

    def resolver(ref):
        if ref not in store:
            raise ConnectorError(f"no secret {ref}")
        return store[ref]

    m = ConnectorManager(
        [{"kind": "slack", "allow": [r".*"], "secret_refs": {"token": "slack_token"}}],
        secret_resolver=resolver,
    )
    creds = m.resolve_credentials("slack")
    assert creds == {"token": "plaintext-secret-value"}
    # the descriptor the engine returns does NOT include the value
    ok, _, conn = m.authorize("slack", "send", "#general")
    assert conn is not None
    plan = conn.execute("send", "#general", {"text": "hi"}, creds)
    assert "token" not in str(plan.get("args", {}))
    assert plan["args"]["channel"] == "general"


def test_missing_secret_refuses_fail_closed():
    def resolver(ref):
        raise ConnectorError(f"no such secret: {ref}")

    m = ConnectorManager(
        [{"kind": "slack", "allow": [r".*"], "secret_refs": {"token": "does_not_exist"}}],
        secret_resolver=resolver,
    )
    with pytest.raises(ConnectorError):
        m.resolve_credentials("slack")


def test_required_credentials_without_resolver_refuses():
    """If a connector needs credentials but no resolver is wired, refuse rather
    than attempt an anonymous authorized call."""
    m = ConnectorManager(
        [{"kind": "slack", "allow": [r".*"], "secret_refs": {"token": "slack_token"}}]
    )
    with pytest.raises(ConnectorError):
        m.resolve_credentials("slack")


def test_describe_never_leaks_secrets():
    m = ConnectorManager(
        [{"kind": "slack", "allow": [r"^general$"], "secret_refs": {"token": "slack_token"}}]
    )
    desc = m.describe()
    assert "slack_token" not in str(desc)
    assert desc["slack"]["credentials_required"] == ["token"]
    assert desc["slack"]["allow"] == ["^general$"]


def test_engine_connector_action_return_never_exposes_secret_value(tmp_path):
    """§20 — the real engine surface the CLI/web consume must never echo a
    resolved credential value. This exercises the full ConnectorManager →
    engine.connector_action return path (not just the unit resolver)."""
    secret_value = "xoxb-REAL-SECRET-VALUE-12345"
    worker = WorkerConfig(
        name="leak-worker",
        tools=[],
        connectors=[
            {
                "kind": "slack",
                "allow": [r".*"],
                "secret_refs": {"token": "slack_token"},
            }
        ],
    )
    store = WorkerStore(str(tmp_path))
    eng = WorkerEngine(worker, store, inference=NullInference())
    # Inject a resolver that returns a known plaintext value, then point the
    # engine's connector manager at it (ConnectorManager captures the resolver
    # at construction time, so we wire it there rather than patching the method).
    resolver = lambda ref: secret_value
    eng.connectors = ConnectorManager(
        specs=worker.connectors, secret_resolver=resolver
    )

    res = eng.connector_action("slack", "send", "#general")
    assert res["ok"] is True
    # the only credential signal is the logical name, never the value
    assert res["credentials_used"] == ["token"]
    assert secret_value not in str(res)
    # JSON serialization (used by --json / web API) is equally clean
    import json

    assert secret_value not in json.dumps(res)
