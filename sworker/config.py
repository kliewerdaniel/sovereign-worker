"""Worker identity + workspace configuration.

A Worker is a YAML file. That is deliberate: the thing that decides what an
autonomous agent is allowed to do should be a diffable, reviewable artifact in
version control, not rows a model can edit.

YAML is parsed by a tiny built-in reader (``_mini_yaml``) so the core has ZERO
third-party dependencies; if PyYAML is installed it is used instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import RiskLevel

DEFAULT_POLICY: Dict[str, str] = {
    "read": "auto",
    "reversible": "auto",
    "external": "approve",
    "financial": "approve",
    "destructive": "approve",
}

POLICY_VALUES = ("auto", "approve", "deny")


@dataclass
class WorkerConfig:
    name: str
    role: str = ""
    instructions: str = ""
    knowledge: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    procedures: List[str] = field(default_factory=list)
    # §20 connectors: default-deny external-system access. Each entry declares a
    # connector kind + allow-list + secret refs. A connector is NEVER active
    # unless explicitly listed here.
    connectors: List[Dict[str, Any]] = field(default_factory=list)
    policy: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    workspace: str = ""
    fs_roots: List[str] = field(default_factory=list)     # writable/readable boundary
    shell_allow: List[str] = field(default_factory=list)  # allowlisted argv[0]
    env_allow: List[str] = field(default_factory=list)    # env vars passed to subprocesses
    max_steps: int = 12
    timeout: int = 60
    max_output: int = 20000
    # resource controls (spec §10) — every limit is enforced; exhaustion is a
    # structured failure, never silent.
    max_runtime: int = 300          # wall-clock seconds for the whole run
    max_actions: int = 24          # total tool actions a run may attempt
    max_tool_calls: int = 100       # total tool invocations (incl. retries)
    max_artifact_bytes: int = 5_000_000
    max_python_runtime: int = 60    # per python.run invocation
    max_shell_runtime: int = 30     # per shell.exec invocation
    max_network_requests: int = 20  # http/network tool invocations per run
    # §21 browser hardening — default-deny on every axis; a worker must opt in.
    browser_allow: List[str] = field(default_factory=list)   # regex allow-list of URLs
    browser_timeout: int = 30       # per-open cap (seconds)
    browser_downloads: bool = False  # allow browser.download into fs boundary
    browser_uploads: bool = False    # allow browser.upload from fs boundary
    browser_credential_refs: List[str] = field(default_factory=list)  # secret refs the browser may inject
    browser_private_session: bool = True  # no shared cookies/profile unless explicitly False
    # §22 messaging policy — default-deny channel + optional rate cap.
    message_allow: List[str] = field(default_factory=list)  # regex allow-list of channels
    message_rate_limit: int = 0  # max messages per run (0 = only bounded by max_actions)
    # §9 execution isolation — which sandbox backend commands run in.
    sandbox: str = "none"  # "none" (host, shallow) | "docker" (container, real isolation)
    # §54 network egress registry — default-deny host allow-list for outbound HTTP.
    egress_allow: List[str] = field(default_factory=list)  # regex host allow-list; empty = deny all
    # §55 DLP primitives — opt-in named detectors run over egress payloads.
    dlp_rules: List[str] = field(default_factory=list)  # names from BUILTIN_DLP_RULES; empty = no scanning
    # §26 worker lifecycle — disabled workers are refused by the engine.
    disabled: bool = False
    # §24 workflow triggers — opt-in automation that launches this worker.
    triggers: List[Dict[str, Any]] = field(default_factory=list)
    # §45 HITL escalation — per-risk approval quorum + minimum role. Shape:
    #   approval_policy: { destructive: {quorum: 2, min_role: operator} }
    # Missing risks fall back to {quorum: 1, min_role: ""} (single approver,
    # any role). This is additive: it never lowers the existing approval gate.
    approval_policy: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    path: str = ""

    def policy_for(self, risk: RiskLevel | str) -> str:
        return self.policy.get(RiskLevel(risk).value, "approve")

    def approval_policy_for(self, risk: RiskLevel | str) -> Dict[str, Any]:
        """§45 — resolve the escalation requirements for a risk level.

        Returns {"quorum": int>=1, "min_role": str}. Absent risks fall back to
        a single approver with no role minimum. Unknown keys are dropped rather
        than trusted.
        """
        raw = self.approval_policy.get(RiskLevel(risk).value, {}) or {}
        quorum = int(raw.get("quorum", 1)) if raw.get("quorum") is not None else 1
        if quorum < 1:
            quorum = 1  # fail-closed: never require zero approvals
        min_role = str(raw.get("min_role", "") or "")
        return {"quorum": quorum, "min_role": min_role}

    def resolved_fs_roots(self) -> List[str]:
        """Absolute, realpath'd boundary roots. Always includes the workspace so
        artifacts and state are writable; never widens beyond declared roots."""
        roots = [os.path.realpath(r) for r in (self.fs_roots or [self.workspace])]
        ws = os.path.realpath(self.workspace)
        if ws not in roots:
            roots.append(ws)
        return roots

    def artifacts_dir(self) -> str:
        d = os.path.join(self.workspace, "artifacts", self.name)
        os.makedirs(d, exist_ok=True)
        return d

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "instructions": self.instructions,
            "knowledge": self.knowledge,
            "tools": self.tools,
            "procedures": self.procedures,
            "connectors": self.connectors,
            "policy": self.policy,
            "workspace": self.workspace,
            "fs_roots": self.fs_roots,
            "shell_allow": self.shell_allow,
            "env_allow": self.env_allow,
            "max_steps": self.max_steps,
            "path": self.path,
            "max_runtime": self.max_runtime,
            "max_actions": self.max_actions,
            "max_tool_calls": self.max_tool_calls,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_python_runtime": self.max_python_runtime,
            "max_shell_runtime": self.max_shell_runtime,
            "max_network_requests": self.max_network_requests,
            "browser_allow": self.browser_allow,
            "browser_timeout": self.browser_timeout,
            "browser_downloads": self.browser_downloads,
            "browser_uploads": self.browser_uploads,
            "browser_credential_refs": self.browser_credential_refs,
            "browser_private_session": self.browser_private_session,
            "message_allow": self.message_allow,
            "message_rate_limit": self.message_rate_limit,
            "sandbox": self.sandbox,
            "egress_allow": self.egress_allow,
            "dlp_rules": self.dlp_rules,
            "disabled": self.disabled,
            "triggers": self.triggers,
            "approval_policy": self.approval_policy,
        }


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    """Everything the platform touches lives under one directory."""

    root: str

    @property
    def workers_dir(self) -> str:
        return os.path.join(self.root, "workers")

    @property
    def state_dir(self) -> str:
        return os.path.join(self.root, ".state")

    @property
    def artifacts_dir(self) -> str:
        return os.path.join(self.root, "artifacts")

    @property
    def procedures_dir(self) -> str:
        return os.path.join(self.root, "procedures")

    @property
    def atlas_dir(self) -> str:
        return os.path.join(self.state_dir, "atlas")

    @property
    def company_dir(self) -> str:
        return os.path.join(self.root, "company")

    def ensure(self) -> "Workspace":
        for d in (
            self.workers_dir,
            self.state_dir,
            self.artifacts_dir,
            self.procedures_dir,
            self.atlas_dir,
            self.company_dir,
        ):
            os.makedirs(d, exist_ok=True)
        return self


def default_workspace() -> Workspace:
    root = os.environ.get("SWORKER_HOME") or os.path.join(os.getcwd(), ".sworker")
    return Workspace(os.path.abspath(root))


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_worker(path: str, workspace: Optional[Workspace] = None) -> WorkerConfig:
    with open(path, "r", encoding="utf-8") as fh:
        data = parse_yaml(fh.read())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: worker file must be a mapping")
    ws = workspace or default_workspace()
    policy = dict(DEFAULT_POLICY)
    for k, v in (data.get("policy") or {}).items():
        key = str(k).strip().lower()
        val = str(v).strip().lower()
        if key not in DEFAULT_POLICY:
            raise ValueError(f"{path}: unknown risk level in policy: {key!r}")
        if val not in POLICY_VALUES:
            raise ValueError(f"{path}: policy {key!r} must be one of {POLICY_VALUES}, got {val!r}")
        policy[key] = val
    base = os.path.dirname(os.path.abspath(path))

    def _abs(p: str) -> str:
        return p if os.path.isabs(p) else os.path.abspath(os.path.join(ws.root, p))

    cfg = WorkerConfig(
        name=str(data.get("name") or os.path.splitext(os.path.basename(path))[0]),
        role=str(data.get("role") or ""),
        instructions=str(data.get("instructions") or ""),
        knowledge=[_abs(str(k)) for k in (data.get("knowledge") or [])],
        tools=[str(t) for t in (data.get("tools") or [])],
        procedures=[str(p) for p in (data.get("procedures") or [])],
        connectors=[dict(c) for c in (data.get("connectors") or [])],
        policy=policy,
        workspace=ws.root,
        fs_roots=[_abs(str(r)) for r in (data.get("fs_roots") or [])] or [ws.root],
        shell_allow=[str(c) for c in (data.get("shell_allow") or [])],
        env_allow=[str(e) for e in (data.get("env_allow") or [])],
        max_steps=int(data.get("max_steps") or 12),
        browser_allow=[str(c) for c in (data.get("browser_allow") or [])],
        browser_timeout=int(data.get("browser_timeout") or 30),
        browser_downloads=bool(data.get("browser_downloads") or False),
        browser_uploads=bool(data.get("browser_uploads") or False),
        browser_credential_refs=[str(c) for c in (data.get("browser_credential_refs") or [])],
        browser_private_session=bool(data.get("browser_private_session")
                                    if data.get("browser_private_session") is not None else True),
        message_allow=[str(c) for c in (data.get("message_allow") or [])],
        message_rate_limit=int(data.get("message_rate_limit") or 0),
        sandbox=str(data.get("sandbox") or "none"),
        egress_allow=[str(c) for c in (data.get("egress_allow") or [])],
        dlp_rules=[str(c) for c in (data.get("dlp_rules") or [])],
        disabled=bool(data.get("disabled") or False),
        triggers=[dict(t) for t in (data.get("triggers") or [])],
        approval_policy={str(k): dict(v) for k, v in (data.get("approval_policy") or {}).items()},
        path=os.path.abspath(path),
    )
    del base
    return cfg


def list_workers(workspace: Optional[Workspace] = None) -> List[WorkerConfig]:
    ws = workspace or default_workspace()
    if not os.path.isdir(ws.workers_dir):
        return []
    out = []
    for name in sorted(os.listdir(ws.workers_dir)):
        if name.endswith((".yaml", ".yml")):
            out.append(load_worker(os.path.join(ws.workers_dir, name), ws))
    return out


def get_worker(name: str, workspace: Optional[Workspace] = None) -> WorkerConfig:
    ws = workspace or default_workspace()
    for ext in (".yaml", ".yml"):
        p = os.path.join(ws.workers_dir, name + ext)
        if os.path.exists(p):
            return load_worker(p, ws)
    known = [w.name for w in list_workers(ws)]
    raise FileNotFoundError(f"no worker named {name!r} in {ws.workers_dir} (have: {known})")


# ---------------------------------------------------------------------------
# yaml
# ---------------------------------------------------------------------------


def parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


def _mini_yaml(text: str) -> Any:
    """Minimal YAML subset: nested maps, '- ' lists, scalars, | block scalars.

    Enough for worker + procedure files. Raises on anything it does not
    understand rather than silently mis-parsing a permission policy.
    """
    lines = text.splitlines()
    pos = 0

    def scalar(tok: str) -> Any:
        tok = tok.strip()
        if not tok:
            return ""
        if tok[0] in "\"'" and tok[-1] == tok[0] and len(tok) > 1:
            return tok[1:-1]
        low = tok.lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        if low in ("null", "~"):
            return None
        try:
            return int(tok)
        except ValueError:
            pass
        try:
            return float(tok)
        except ValueError:
            pass
        return tok

    def indent_of(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" "))

    def block(min_indent: int) -> Any:
        nonlocal pos
        result: Any = None
        while pos < len(lines):
            raw = lines[pos]
            if not raw.strip() or raw.lstrip().startswith("#"):
                pos += 1
                continue
            ind = indent_of(raw)
            if ind < min_indent:
                break
            body = raw.strip()
            if body.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    break
                item = body[2:].strip()
                pos += 1
                if ":" in item and not item.startswith(("\"", "'")):
                    k, _, v = item.partition(":")
                    sub = {k.strip(): scalar(v)} if v.strip() else {}
                    if not v.strip():
                        sub[k.strip()] = block(ind + 2)
                    nxt = block(ind + 2)
                    if isinstance(nxt, dict):
                        sub.update(nxt)
                    result.append(sub)
                else:
                    result.append(scalar(item))
                continue
            if ":" not in body:
                raise ValueError(f"cannot parse YAML line: {raw!r}")
            key, _, rest = body.partition(":")
            key = key.strip()
            rest = rest.strip()
            if result is None:
                result = {}
            if not isinstance(result, dict):
                break
            pos += 1
            if rest in ("|", ">"):
                buf = []
                while pos < len(lines) and (not lines[pos].strip() or indent_of(lines[pos]) > ind):
                    buf.append(lines[pos][ind + 2 :] if lines[pos].strip() else "")
                    pos += 1
                joined = "\n".join(buf).rstrip()
                result[key] = joined if rest == "|" else " ".join(joined.split())
            elif rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                result[key] = [scalar(x) for x in inner.split(",")] if inner else []
            elif rest:
                result[key] = scalar(rest)
            else:
                result[key] = block(ind + 1)
        return result

    parsed = block(0)
    return {} if parsed is None else parsed
