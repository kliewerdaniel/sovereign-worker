"""Shell + Python execution.

Honest security note: by default these run as SUBPROCESSES on the host (sandbox
``none``), not in a VM. The boundaries enforced are real but shallow — see
docs/SECURITY.md (section 2). For genuine isolation set ``sandbox: docker`` on a
worker; the §9 ``Sandbox`` abstraction will run the command in a container and, if
docker is unavailable, FAIL CLOSED rather than silently downgrading to host
execution.

Both tools route through :mod:`sworker.tools.sandbox`, which is the single place
that owns the isolation boundary (env allow-list, fs boundary via cwd, timeout,
process-group kill). The tools only do *policy* (which command/argv is allowed);
the sandbox owns *isolation*.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from typing import Any, Dict

from ..models import RiskLevel
from .base import Tool, ToolContext, ToolError, ToolResult
from .sandbox import SandboxError, run_in_sandbox


class ShellExec(Tool):
    name = "shell.exec"
    description = (
        "Run an allowlisted command. Parsed with shlex and executed WITHOUT a shell, "
        "so pipes/redirection/globs are not interpreted. Runs inside the worker's "
        "configured sandbox (default: host subprocess)."
    )
    risk = RiskLevel.REVERSIBLE
    permissions = ["subprocess"]
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["command"],
    }

    def summarize(self, args: Dict[str, Any]) -> str:
        return f"run shell command: {args.get('command')}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        try:
            argv = shlex.split(args["command"])
        except ValueError as exc:
            raise ToolError(f"cannot parse command: {exc}") from exc
        if not argv:
            raise ToolError("empty command")
        if not ctx.shell_allow:
            raise ToolError(
                "this worker has no shell_allow list; shell execution is denied by default"
            )
        base = os.path.basename(argv[0])
        if base not in ctx.shell_allow and argv[0] not in ctx.shell_allow:
            raise ToolError(
                f"command {base!r} is not in this worker's shell_allow list {ctx.shell_allow}"
            )
        timeout = min(int(args.get("timeout", 30)), min(300, ctx.max_shell_runtime))
        return run_in_sandbox(argv, ctx, timeout)


class PythonAnalysis(Tool):
    name = "python.run"
    description = (
        "Run a Python analysis script in the worker's sandbox. The script may print a "
        "line starting with 'RESULT_JSON:' followed by a JSON object; that object is "
        "captured as structured output and becomes machine-checkable evidence."
    )
    risk = RiskLevel.REVERSIBLE
    permissions = ["subprocess"]
    input_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer", "default": 60},
        },
        "required": ["code"],
    }

    def summarize(self, args: Dict[str, Any]) -> str:
        first = (args.get("code") or "").strip().splitlines()[:1]
        return f"run python analysis ({len(args.get('code',''))} chars): {first[0] if first else ''}"

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        scripts = os.path.join(ctx.workspace, ".state", "scripts")
        os.makedirs(scripts, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".py", dir=scripts, prefix=f"{ctx.run_id}_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(args["code"])
        timeout = min(int(args.get("timeout", 60)), min(300, ctx.max_python_runtime))
        res = run_in_sandbox([sys.executable, path], ctx, timeout)
        structured = None
        for line in (res.data.get("stdout") or "").splitlines():
            if line.startswith("RESULT_JSON:"):
                try:
                    structured = json.loads(line[len("RESULT_JSON:") :].strip())
                except json.JSONDecodeError:
                    structured = None
        res.data["script"] = path
        res.data["sandbox"] = res.data.get("sandbox", ctx.sandbox)
        if structured is not None:
            res.data["result"] = structured
            res.evidence.append(
                {
                    "source_ref": f"python.run:{os.path.basename(path)}",
                    "excerpt": json.dumps(structured, sort_keys=True)[:400],
                }
            )
        return res


TOOLS = [ShellExec(), PythonAnalysis()]
