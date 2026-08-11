"""Tool interface and registry.

A Tool is metadata + a pure-ish callable. The engine — never the model — decides
whether a tool may run, so risk/permission data lives on the tool class and is
not something the model can talk its way around.

Contract:
    validate(args) -> normalised args  (raises ToolError on bad input)
    run(ctx, args) -> ToolResult       (never raises for expected failures)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from ..models import RiskLevel


class ToolError(Exception):
    """Invalid invocation (bad/missing args, boundary violation)."""


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    artifacts: List[str] = field(default_factory=list)   # absolute paths produced
    evidence: List[Dict[str, Any]] = field(default_factory=list)  # explicit evidence refs


@dataclass
class ToolContext:
    """What a tool is allowed to touch. Constructed by the engine from the
    WorkerConfig — a tool can never widen its own boundary."""

    worker: str
    run_id: str
    workspace: str
    fs_roots: List[str]
    artifacts_dir: str
    shell_allow: List[str] = field(default_factory=list)
    env_allow: List[str] = field(default_factory=list)
    timeout: int = 30
    max_output: int = 20000
    # live child PIDs registered by the exec tools during a run, so the engine
    # can kill the whole process group on cancel (spec §11).
    _live_pids: set = field(default_factory=set, repr=False, compare=False)
    # per-invocation timeouts (spec §10); the engine sets these from the worker
    max_python_runtime: int = 60
    max_shell_runtime: int = 30
    # §21 browser hardening — default-deny on every axis. The engine fills these
    # from WorkerConfig; a tool can never widen them.
    browser_allow: List[str] = field(default_factory=list)   # regex allow-list of URLs
    browser_timeout: int = 30
    browser_downloads: bool = False   # whether browser.download is permitted
    browser_uploads: bool = False     # whether browser.upload is permitted
    browser_credential_refs: List[str] = field(default_factory=list)  # secret refs the browser may inject
    browser_private_session: bool = True
    secret_resolver: Optional[Callable[[str], Optional[str]]] = None  # resolves §8 refs to plaintext
    # §22 messaging policy — default-deny channel allow-list + per-run rate cap.
    message_allow: List[str] = field(default_factory=list)
    message_rate_limit: int = 0
    messages_sent: int = 0  # engine/tool shared counter for rate limiting within a run
    # §9 execution isolation — which sandbox backend commands run in.
    sandbox: str = "none"  # "none" | "docker"
    # §54 network egress registry — default-deny host allow-list for outbound HTTP.
    egress_allow: List[str] = field(default_factory=list)
    # §55 DLP primitives — opt-in named detectors (BUILTIN_DLP_RULES) run over
    # egress payloads. Empty = no scanning. The engine compiles these into a
    # DlpPolicy; a tool can never widen its own boundary.
    dlp_rules: List[str] = field(default_factory=list)

    def register_subprocess(self, pid: int) -> None:
        self._live_pids.add(pid)

    def unregister_subprocess(self, pid: int) -> None:
        self._live_pids.discard(pid)

    @property
    def running_subprocesses(self) -> frozenset:
        return frozenset(self._live_pids)

    def resolve(self, path: str, *, must_exist: bool = False) -> str:
        """Resolve a path and prove it is inside a permitted root.

        Uses realpath on BOTH sides so a symlink out of the sandbox is caught.
        """
        p = os.path.realpath(os.path.join(self.workspace, os.path.expanduser(path)))
        for root in self.fs_roots:
            r = os.path.realpath(root)
            if p == r or p.startswith(r + os.sep):
                if must_exist and not os.path.exists(p):
                    raise ToolError(f"path does not exist: {path}")
                return p
        raise ToolError(
            f"path {path!r} is outside this worker's filesystem boundary "
            f"({', '.join(self.fs_roots)})"
        )

    def clean_env(self) -> Dict[str, str]:
        """Env for subprocesses: allowlist only. Secrets do not leak by default."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.workspace,
            "LANG": "C.UTF-8",
            "SWORKER_RUN_ID": self.run_id,
        }
        for k in self.env_allow:
            if k in os.environ:
                env[k] = os.environ[k]
        return env


class Tool:
    name: str = ""
    description: str = ""
    input_schema: Dict[str, Any] = {}
    output_schema: Dict[str, Any] = {"type": "object"}
    risk: RiskLevel = RiskLevel.READ
    reversible: bool = True
    requires_approval: bool = False   # force approval regardless of policy
    permissions: List[str] = []
    categories: List[str] = []        # e.g. ["network"] — used for run-level
                                      # resource accounting (spec §10)

    # -- metadata ----------------------------------------------------------
    @classmethod
    def spec(cls) -> Dict[str, Any]:
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": cls.input_schema,
            "output_schema": cls.output_schema,
            "risk_level": cls.risk.value,
            "permissions": cls.permissions,
            "reversible": cls.reversible,
            "requires_approval": cls.requires_approval,
            "categories": cls.categories,
        }

    # -- behaviour ---------------------------------------------------------
    def validate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Schema check. Deliberately tiny — a real JSON-Schema lib is overkill
        for the handful of primitive types the tools use."""
        out: Dict[str, Any] = {}
        schema = self.input_schema or {}
        props: Dict[str, Any] = schema.get("properties", {})
        required = schema.get("required", [])
        unknown = set(args) - set(props)
        if unknown:
            raise ToolError(f"{self.name}: unknown argument(s): {sorted(unknown)}")
        for key in required:
            if key not in args or args[key] in (None, ""):
                raise ToolError(f"{self.name}: missing required argument {key!r}")
        for key, spec in props.items():
            if key not in args:
                if "default" in spec:
                    out[key] = spec["default"]
                continue
            val = args[key]
            want = spec.get("type")
            if want == "string" and not isinstance(val, str):
                val = str(val)
            elif want == "integer":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise ToolError(f"{self.name}: {key!r} must be an integer, got {val!r}")
            elif want == "number":
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    raise ToolError(f"{self.name}: {key!r} must be a number, got {val!r}")
            elif want == "boolean":
                val = bool(val)
            elif want == "array" and not isinstance(val, list):
                raise ToolError(f"{self.name}: {key!r} must be an array, got {type(val).__name__}")
            elif want == "object" and not isinstance(val, dict):
                raise ToolError(f"{self.name}: {key!r} must be an object, got {type(val).__name__}")
            if "enum" in spec and val not in spec["enum"]:
                raise ToolError(f"{self.name}: {key!r} must be one of {spec['enum']}, got {val!r}")
            out[key] = val
        return out

    def run(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    def summarize(self, args: Dict[str, Any]) -> str:
        """One-line human description, used in approval prompts and audit."""
        pairs = ", ".join(f"{k}={v!r}" for k, v in sorted(args.items()))
        return f"{self.name}({pairs})"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool must have a name")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"unknown tool: {name!r} (have: {sorted(self._tools)})")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return sorted(self._tools)

    def specs(self) -> List[Dict[str, Any]]:
        return [t.spec() for t in sorted(self._tools.values(), key=lambda t: t.name)]

    def subset(self, names: List[str]) -> "ToolRegistry":
        """Registry restricted to what a worker is configured for.

        Supports 'family.*' globs so a worker can say `fs.*`.
        """
        r = ToolRegistry()
        for n in names:
            if n.endswith(".*"):
                prefix = n[:-1]
                for k, t in self._tools.items():
                    if k.startswith(prefix):
                        r.register(t)
            elif n == "*":
                for t in self._tools.values():
                    r.register(t)
            else:
                r.register(self.get(n))
        return r


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated at {limit} chars]", True
