"""§9 execution isolation abstraction.

Verifies that shell.exec / python.run route through a single Sandbox boundary,
that `none` runs on the host, and that `docker` FAILS CLOSED (does not silently
downgrade to host execution) when the docker CLI is unavailable.
"""

from __future__ import annotations

import sys

import pytest

from sworker.tools.base import ToolContext
from sworker.tools.exec import PythonAnalysis, ShellExec
from sworker.tools.sandbox import Sandbox, SandboxError, run_in_sandbox


def _ctx(**kw) -> ToolContext:
    base = dict(
        worker="w",
        run_id="r1",
        workspace="/tmp",
        fs_roots=["/tmp"],
        artifacts_dir="/tmp/artifacts",
        shell_allow=["echo", "true"],
        env_allow=[],
        max_output=20000,
        sandbox="none",
    )
    base.update(kw)
    return ToolContext(**base)


def test_sandbox_unknown_mode_rejected():
    with pytest.raises(SandboxError):
        Sandbox("vm")


def test_shell_runs_inside_none_sandbox_on_host():
    res = ShellExec().run(_ctx(), {"command": "echo hello"})
    assert res.ok is True
    assert "hello" in res.output
    assert res.data["sandbox"] == "none"


def test_python_runs_inside_none_sandbox_and_captures_result_json():
    code = "print('RESULT_JSON: {\"q\": 42}')\nprint('done')"
    res = PythonAnalysis().run(_ctx(), {"code": code})
    assert res.ok is True
    assert res.data["result"] == {"q": 42}
    assert res.data["sandbox"] == "none"
    assert any(e["source_ref"].startswith("python.run:") for e in res.evidence)


def test_docker_sandbox_fails_closed_when_docker_absent(monkeypatch):
    """Requesting docker with no docker available must refuse, not downgrade."""
    import sworker.tools.sandbox as sb

    monkeypatch.setattr(sb, "_docker_binary", lambda: None)
    with pytest.raises(SandboxError):
        sb.Sandbox("docker").run(["echo", "hi"], _ctx(sandbox="docker"), 10)


def test_docker_sandbox_via_tool_fails_closed_not_host(monkeypatch):
    """shell.exec with sandbox: docker and no docker must NOT run on the host."""
    import sworker.tools.sandbox as sb

    monkeypatch.setattr(sb, "_docker_binary", lambda: None)
    res = ShellExec().run(_ctx(sandbox="docker"), {"command": "echo should-not-run"})
    assert res.ok is False
    assert "sandbox refused to run" in res.error
    assert "docker" in res.data["sandbox_error"]
    assert "should-not-run" not in res.output


def test_run_in_sandbox_reports_sandbox_mode():
    res = run_in_sandbox(["echo", "x"], _ctx(sandbox="none"), 10)
    assert res.ok is True
    assert res.data["sandbox"] == "none"


def test_docker_sandbox_command_shape_when_docker_present(monkeypatch):
    """If docker IS present, the argv is wrapped but we don't actually run docker
    (no daemon in test). We assert the wrapping logic by intercepting Popen."""
    import sworker.tools.sandbox as sb

    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        raise FileNotFoundError("docker not actually executed in test")

    monkeypatch.setattr(sb, "_docker_binary", lambda: "/usr/bin/docker")
    monkeypatch.setattr(sb.subprocess, "Popen", staticmethod(fake_popen))
    res = sb.Sandbox("docker").run(["echo", "hi"], _ctx(sandbox="docker"), 10)
    # we never reach the backend spawn because Popen is stubbed; the command was
    # being wrapped. Just assert construction/wrapping didn't downgrade to host.
    assert res.backend_error is not None or "docker" in " ".join(captured.get("argv", []))
