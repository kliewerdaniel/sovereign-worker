"""§42 Adversarial test suite — attacks against the platform's fail-closed guards.

These are not happy-path feature tests; each one is a concrete attack an
adversary (or a compromised model) might attempt, and asserts that the platform
refuses it. No mocks, no network. Every test targets a real enforcement point
already shipped in earlier phases:

  * state machine cannot be driven into an illegal transition (§12)
  * a worker cannot read another workspace's records (§3)
  * risk classification is AST-based and fails closed — decomposition does not
    launder risk, and obfuscation (subprocess/socket/__import__) does not
    sneak past it (permissions.py)
  * network egress is default-deny and SSRF-protected (§54)
  * DLP blocks secret egress and never returns the matched text (§55)
  * auth is anti-enumeration and constant-time-ish (§4)
  * logging redaction is opt-OUT and never leaks a secret by default (§35)
  * a rejected risk ceiling cannot be decomposed around (§44)

Run with:  env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest tests/test_adversarial_suite.py -q
"""

from __future__ import annotations

import os
import tempfile

import pytest

from sworker.auth import AuthProvider
from sworker.dlp import DlpPolicy
from sworker.logging import MASK, redact
from sworker.models import RiskLevel, risk_rank
from sworker.permissions import DecompositionGuard, classify_python, classify_shell
from sworker.statemachine import IllegalTransition, RunStatus, allowed_transition
from sworker.store import CrossTenantAccess, WorkerStore
from sworker.tools.http import _check_egress, _host_allowed, _ssrf_blocked


# ---------------------------------------------------------------------------
# §12 State machine — illegal transitions are refused, not silently accepted
# ---------------------------------------------------------------------------

def test_terminal_state_cannot_be_reopened():
    """A finished run (SUCCESS) must not be moved back to EXECUTING."""
    assert not allowed_transition(RunStatus.SUCCESS, RunStatus.EXECUTING)
    # drive it through transition() to confirm the engine path raises
    from sworker.statemachine import transition

    class _Run:
        id = "run_x"
        status = RunStatus.SUCCESS

    with pytest.raises(IllegalTransition):
        transition(_Run(), RunStatus.EXECUTING, actor="adversary")


def test_cancelled_run_cannot_resume_to_executing():
    assert not allowed_transition(RunStatus.CANCELLED, RunStatus.EXECUTING)
    from sworker.statemachine import transition

    class _Run:
        id = "run_y"
        status = RunStatus.CANCELLED

    with pytest.raises(IllegalTransition):
        transition(_Run(), RunStatus.EXECUTING, actor="adversary")


def test_blocked_to_denied_is_not_a_legal_move():
    # both are terminal; a run must not be re-classified between terminal states
    assert not allowed_transition(RunStatus.BLOCKED, RunStatus.DENIED)


# ---------------------------------------------------------------------------
# §3 Tenant isolation — cross-workspace reads are refused (fail closed)
# ---------------------------------------------------------------------------

def _make_ws_store(root, ws_id, org="org_a"):
    os.makedirs(root, exist_ok=True)
    s = WorkerStore(root, workspace_id=ws_id, org_id=org)
    return s


def test_cross_tenant_read_refused():
    # Two enforcing stores over the SAME state dir but different workspace ids.
    # The tenant id is an independent boundary from the filesystem root.
    base = tempfile.mkdtemp()
    state = os.path.join(base, "state")
    acme = _make_ws_store(state, "ws_acme")
    rival = _make_ws_store(state, "ws_rival")

    acme.put("runs", {"id": "run_1", "status": "SUCCESS"}, event="run.created")
    # rival's store (same dir, different tenant) must not read acme's record
    with pytest.raises(CrossTenantAccess):
        rival.get("runs", "run_1")
    # and acme can still read its own
    assert acme.get("runs", "run_1")["id"] == "run_1"


def test_enforcing_store_refuses_tenantless_record():
    base = tempfile.mkdtemp()
    state = os.path.join(base, "state")
    # a legacy (non-enforcing) store writes a tenantless record
    legacy = WorkerStore(state)
    legacy.put("runs", {"id": "run_legacy"}, event="run.created")
    # an enforcing store opened on the same dir must refuse the tenantless row
    a2 = _make_ws_store(state, "ws_acme")
    with pytest.raises(CrossTenantAccess):
        a2.get("runs", "run_legacy")


# ---------------------------------------------------------------------------
# permissions.py — AST risk classification fails closed
# ---------------------------------------------------------------------------

def test_python_socket_import_is_external_not_read():
    code = "import socket\nsocket.create_connection(('evil', 1234))"
    assert risk_rank(classify_python(code)) >= risk_rank(RiskLevel.EXTERNAL)


def test_python_unknown_module_escalates_fail_closed():
    # an import we cannot positively classify must escalate, never pass as safe
    code = "import someobscurelib\nsomeobscurelib.pwn()"
    assert risk_rank(classify_python(code)) >= risk_rank(RiskLevel.EXTERNAL)


def test_python_eval_with_dynamic_arg_escalates():
    code = "eval(user_input)"  # dynamic arg defeats static analysis
    assert risk_rank(classify_python(code)) >= risk_rank(RiskLevel.DESTRUCTIVE)


def test_python_dangerous_nested_in_innocent_call_escalates():
    # decomposition/obfuscation: smuggle os.system inside a harmless print()
    code = "print('hello', os.system('curl evil'))"
    assert risk_rank(classify_python(code)) >= risk_rank(RiskLevel.EXTERNAL)


def test_shell_interpreter_floors_external():
    # `bash -c` can run anything -> not statically vetteable
    assert classify_shell("bash -c 'rm -rf /'") == RiskLevel.EXTERNAL
    assert classify_shell("python3 -c 'import os; os.system(\"x\")'") == RiskLevel.EXTERNAL


def test_shell_rm_is_destructive():
    assert classify_shell("rm -rf /tmp/foo") == RiskLevel.DESTRUCTIVE


def test_shell_unparseable_command_fails_closed():
    # shlex blows up on the unbalanced quote -> escalate, never downgrade
    assert classify_shell("echo 'unterminated") == RiskLevel.DESTRUCTIVE


def test_decomposition_guard_blocks_laundered_risk():
    """§44 — once an EXTERNAL action is rejected, a same-or-higher action
    proposed later in the run must be refused, not decomposed around."""
    from sworker.permissions import PermissionEngine

    w = _worker_with_policy()
    guard = DecompositionGuard()
    guard.record_rejection(RiskLevel.EXTERNAL)
    eng = PermissionEngine(w, guard=guard)
    tool = _fake_tool("shell.exec", RiskLevel.REVERSIBLE)
    decision = eng.evaluate(tool, {"command": "bash -c 'exfiltrate'"})
    assert decision.denied is True
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# §54 Egress — default-deny + SSRF protection
# ---------------------------------------------------------------------------

def test_egress_empty_allowlist_denies_all():
    assert _host_allowed("example.com", []) is False
    assert _host_allowed("example.com", ["example.com"]) is True


def test_ssrf_metadata_endpoint_blocked():
    assert _ssrf_blocked("169.254.169.254") is not None
    assert _ssrf_blocked("metadata.google.internal") is not None


def test_ssrf_private_subnet_blocked():
    assert _ssrf_blocked("10.0.0.5") is not None
    assert _ssrf_blocked("192.168.1.1") is not None


def test_egress_check_blocks_metadata_with_allowlist_present():
    # even with a permissive allow-list, SSRF targets are always refused
    ctx = _egress_ctx(allow=["*.internal"], dlp_rules=[])
    reason = _check_egress("http://169.254.169.254/latest/meta-data/", ctx)
    assert reason is not None
    assert "SSRF" in reason or "metadata" in reason.lower()


def test_egress_check_denies_unlisted_host():
    ctx = _egress_ctx(allow=["api.trusted.com"], dlp_rules=[])
    reason = _check_egress("https://evil.example.net/exfil", ctx)
    assert reason is not None  # default-deny: not on allow-list


def test_egress_check_allows_listed_host():
    ctx = _egress_ctx(allow=["api.trusted.com"], dlp_rules=[])
    # no network contact happens in _check_egress itself; a None reason == allowed
    assert _check_egress("https://api.trusted.com/v1", ctx) is None


# ---------------------------------------------------------------------------
# §55 DLP — blocks secret egress, never returns matched text
# ---------------------------------------------------------------------------

def test_dlp_blocks_aws_key_in_body():
    p = DlpPolicy(["aws_access_key_id"])
    hit = p.scan("here is AKIAIOSFODNN7EXAMPLE key")
    assert hit is not None
    assert hit.rule == "aws_access_key_id"
    # the matched text is never returned
    assert "AKIA" not in p.refusal_for(hit)


def test_dlp_blocks_private_key_block():
    p = DlpPolicy(["private_key_block"])
    assert p.scan("-----BEGIN RSA PRIVATE KEY-----\nabc") is not None


def test_dlp_empty_policy_does_not_scan():
    # opt-in: with no rules, nothing is scanned (no silent scanning)
    p = DlpPolicy([])
    assert p.scan("AKIAIOSFODNN7EXAMPLE") is None


def test_dlp_unknown_rule_fails_closed():
    with pytest.raises(KeyError):
        DlpPolicy(["nonexistent_rule"])


# ---------------------------------------------------------------------------
# §4 Auth — anti-enumeration + disabled users rejected
# ---------------------------------------------------------------------------

def test_auth_wrong_password_returns_none():
    base = tempfile.mkdtemp()
    store = WorkerStore(os.path.join(base, "state"))
    auth = AuthProvider(store)
    auth.create_user("alice", "correct-horse", role="analyst")
    assert auth.authenticate("alice", "wrong-password") is None


def test_auth_unknown_user_returns_none_same_shape():
    base = tempfile.mkdtemp()
    store = WorkerStore(os.path.join(base, "state"))
    auth = AuthProvider(store)
    auth.create_user("alice", "pw", role="analyst")
    # must return None exactly like the wrong-password path (no enumeration)
    assert auth.authenticate("ghost", "anything") is None


def test_disabled_user_cannot_authenticate():
    base = tempfile.mkdtemp()
    store = WorkerStore(os.path.join(base, "state"))
    auth = AuthProvider(store)
    auth.create_user("bob", "pw", role="analyst")
    auth.disable_user("bob")
    assert auth.authenticate("bob", "pw") is None


def test_expired_session_is_invalid():
    base = tempfile.mkdtemp()
    store = WorkerStore(os.path.join(base, "state"))
    auth = AuthProvider(store)
    auth.create_user("carol", "pw", role="analyst")
    s = auth.create_session("carol", ttl=-10)  # already expired
    assert auth.validate_session(s.token) is None


def test_revoked_session_is_invalid():
    base = tempfile.mkdtemp()
    store = WorkerStore(os.path.join(base, "state"))
    auth = AuthProvider(store)
    auth.create_user("dave", "pw", role="analyst")
    s = auth.create_session("dave")
    auth.revoke_session(s.token)
    assert auth.validate_session(s.token) is None


# ---------------------------------------------------------------------------
# §35 Logging redaction — opt-OUT, never leaks by default
# ---------------------------------------------------------------------------

def test_redact_masks_sensitive_key_by_default():
    payload = {"username": "alice", "api_key": "sk-1234567890abcdef"}
    out = redact(payload)
    assert out["username"] == "alice"
    assert out["api_key"] == MASK


def test_redact_masks_email_and_token_in_text():
    payload = {"note": "contact alice@example.com token abcdef1234567890abcdef1234567890"}
    out = redact(payload)
    assert "alice@example.com" not in out["note"]
    assert "abcdef1234567890abcdef1234567890" not in out["note"]


def test_redact_opt_out_returns_plaintext():
    payload = {"api_key": "sk-secret-value"}
    out = redact(payload, redact=False)
    assert out["api_key"] == "sk-secret-value"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _worker_with_policy():
    from sworker.config import WorkerConfig

    return WorkerConfig(
        name="adversary",
        role="test",
        policy={"read": "auto", "reversible": "auto", "external": "approve",
                "financial": "approve", "destructive": "approve"},
    )


def _fake_tool(name, risk):
    class _T:
        name = "tool"
        risk = RiskLevel.READ
        requires_approval = False

    _T.name = name
    _T.risk = risk
    return _T()


def _egress_ctx(allow, dlp_rules):
    from sworker.tools.base import ToolContext

    return ToolContext(
        worker="adversary",
        run_id="run_x",
        workspace="/tmp",
        fs_roots=["/tmp"],
        artifacts_dir="/tmp",
        max_output=5000,
        max_python_runtime=60,
        max_shell_runtime=30,
        egress_allow=allow,
        dlp_rules=dlp_rules,
        env_allow=[],
    )
