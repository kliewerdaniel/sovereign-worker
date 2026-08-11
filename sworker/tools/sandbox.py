"""§9 execution isolation abstraction.

Both ``shell.exec`` and ``python.run`` run through a single ``Sandbox`` boundary.
This is the one place that decides *how* a command is executed on the host, so
all isolation guarantees (process-group kill, env allow-list, fs boundary,
timeout) live here and cannot drift between the two tools.

Two backends:

  * ``none``  — runs on the host as a subprocess. The boundaries (allow-list,
    env allow-list, fs boundary, timeout, process-group kill) are real but
    shallow; this is NOT a security boundary against a determined process. This
    is the default and requires no external dependency.
  * ``docker`` — wraps the command in ``docker run --rm`` with the workspace
    bind-mounted read-write and a read-only copy of declared env vars. Real
    isolation. If docker is not available the sandbox FAILS CLOSED (refuses to
    run) instead of silently downgrading to host execution — downgrading would
    quietly weaken isolation, which is exactly the failure we must not hide.

The host backend is always available; the docker backend is opt-in and degrades
closed. No third-party dependency is required for either path (docker is
invoked via the ``docker`` CLI if present).
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import ToolContext, ToolError, ToolResult, truncate


class SandboxError(RuntimeError):
    """Raised when a sandbox cannot be constructed or refuses to run."""


def _terminate_group(proc: "subprocess.Popen") -> None:
    """Kill the whole process group (children included). Best-effort."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timeout: bool
    duration_ms: int
    argv: List[str]
    sandbox: str
    backend_error: Optional[str] = None


class Sandbox:
    """Executes argv inside a chosen isolation backend.

    Construct via ``Sandbox.for_worker(ctx)`` — it picks the backend from
    ``ctx.sandbox`` and fails closed if an unavailable backend is requested.
    """

    def __init__(self, mode: str):
        if mode not in ("none", "docker"):
            raise SandboxError(f"unknown sandbox mode {mode!r} (want 'none' or 'docker')")
        self.mode = mode

    @classmethod
    def for_worker(cls, ctx: ToolContext) -> "Sandbox":
        return cls(ctx.sandbox)

    # -- the boundary shared by every backend -------------------------------
    def _spawn(self, argv: List[str], ctx: ToolContext, timeout: int) -> RunResult:
        t0 = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=ctx.workspace,
                env=ctx.clean_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # new pgid so cancel kills the whole tree
            )
        except FileNotFoundError as exc:
            return RunResult(False, -1, "", "", False, 0, argv, self.mode,
                             backend_error=f"command not found: {argv[0] if argv else ''} ({exc})")
        ctx.register_subprocess(proc.pid)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_group(proc)
                out, err = proc.communicate()
                return RunResult(False, proc.returncode or -1, out, err, True,
                                 int((time.time() - t0) * 1000), argv, self.mode)
        finally:
            ctx.unregister_subprocess(proc.pid)
        return RunResult(
            proc.returncode == 0, proc.returncode or 0, out, err, False,
            int((time.time() - t0) * 1000), argv, self.mode,
        )

    def run(self, argv: List[str], ctx: ToolContext, timeout: int) -> RunResult:
        if self.mode == "docker":
            return self._run_docker(argv, ctx, timeout)
        return self._spawn(argv, ctx, timeout)

    # -- docker backend (opt-in, fails closed) ------------------------------
    def _run_docker(self, argv: List[str], ctx: ToolContext, timeout: int) -> RunResult:
        docker = _docker_binary()
        if docker is None:
            # FAIL CLOSED: never silently fall back to host execution.
            raise SandboxError(
                "sandbox: docker requested but the 'docker' CLI is not available; "
                "refusing to downgrade to host execution. Set sandbox: none or "
                "install docker."
            )
        # bind the workspace read-write; rootfs outside it is not mounted.
        docker_argv = [
            docker, "run", "--rm",
            "--network", "none" if not _env_network_allowed(ctx) else "bridge",
            "-w", "/work",
            "-v", f"{ctx.workspace}:/work:rw",
            # drop privileges; the worker runs as nobody inside the container
            "--user", "65534:65534",
            "--cap-drop", "ALL",
        ]
        for k, v in ctx.clean_env().items():
            docker_argv += ["-e", f"{k}={v}"]
        docker_argv += ["sworker-exec", *argv]
        return self._spawn(docker_argv, ctx, timeout)


def _docker_binary() -> Optional[str]:
    from shutil import which

    return which("docker")


def _env_network_allowed(ctx: ToolContext) -> bool:
    """Network inside the container is only granted if a network tool is in play;
    for pure shell/python analysis we default to --network none."""
    return False


def run_in_sandbox(argv: List[str], ctx: ToolContext, timeout: int) -> ToolResult:
    """Shared exec path for shell.exec / python.run. Wraps ``Sandbox`` and
    converts a ``RunResult`` into a ``ToolResult`` with the existing shape."""
    sb = Sandbox.for_worker(ctx)
    try:
        res = sb.run(argv, ctx, timeout)
    except SandboxError as exc:
        return ToolResult(False, error=f"sandbox refused to run: {exc}",
                          data={"sandbox": ctx.sandbox, "sandbox_error": str(exc)})
    out, t1 = truncate(res.stdout, ctx.max_output)
    err, t2 = truncate(res.stderr, ctx.max_output)
    combined = out if not err else f"{out}\n[stderr]\n{err}"
    return ToolResult(
        res.ok,
        output=combined.strip(),
        error="" if res.ok else f"exit {res.exit_code}: {err.strip()[:500]}",
        truncated=t1 or t2,
        data={
            "argv": res.argv,
            "exit_code": res.exit_code,
            "stdout": out,
            "stderr": err,
            "duration_ms": res.duration_ms,
            "timeout": res.timeout,
            "sandbox": res.sandbox,
            "backend_error": res.backend_error,
        },
    )
