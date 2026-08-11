"""Execution objects.

Every meaningful thing the Worker does becomes an explicit, persisted object.
The LLM never performs a consequential action "inside a thought" — it can only
emit an Action, which the engine must route through permissions, approval and
verification before it is executed.

Lifecycle:

    Task -> Plan -> Step -> Action -> Observation -> Evidence
         -> Claim -> Verification -> Artifact -> Approval -> Run(status)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict, fields
from enum import Enum
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    """Ordered least -> most consequential. Order is load-bearing: the approval
    guard compares levels, so never reorder without updating RISK_ORDER."""

    READ = "read"
    REVERSIBLE = "reversible"
    EXTERNAL = "external"
    FINANCIAL = "financial"
    DESTRUCTIVE = "destructive"


RISK_ORDER = [
    RiskLevel.READ,
    RiskLevel.REVERSIBLE,
    RiskLevel.EXTERNAL,
    RiskLevel.FINANCIAL,
    RiskLevel.DESTRUCTIVE,
]


def risk_rank(r: "RiskLevel | str") -> int:
    return RISK_ORDER.index(RiskLevel(r))


class RunStatus(str, Enum):
    """Failure is a first-class result. There is deliberately no 'unknown'.

    The lifecycle a run passes through (spec §12), enforced by
    ``sworker.statemachine``:

        PENDING -> PLANNING -> EXECUTING -> {AWAITING_APPROVAL} -> VERIFYING
                 -> SUCCESS | PARTIAL_SUCCESS | FAILED | BLOCKED
                 | INSUFFICIENT_EVIDENCE | CANCELLED | DENIED

    Terminal states (no outgoing transition): SUCCESS, PARTIAL_SUCCESS, FAILED,
    BLOCKED, INSUFFICIENT_EVIDENCE, CANCELLED, DENIED.
    """

    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    EXECUTING = "EXECUTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CANCELLED = "CANCELLED"
    DENIED = "DENIED"


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DENIED = "DENIED"          # policy said no; no human was ever asked
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SKIPPED = "SKIPPED"


class Provenance(str, Enum):
    """How the Worker came to hold something. Never collapse these: the whole
    point is that 'the model said so' and 'the CSV said so' are different."""

    KNOWN = "known"            # in the worker's own instructions/config
    RETRIEVED = "retrieved"    # from compiled company knowledge
    OBSERVED = "observed"      # returned by a tool
    INFERRED = "inferred"      # derived deterministically from other evidence
    HYPOTHESIZED = "hypothesized"  # model-generated, unverified
    VERIFIED = "verified"      # deterministically re-checked


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class VerificationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"


class ApprovalState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


class Record:
    """Mixin: dataclass <-> dict with enums flattened to their values."""

    def to_dict(self) -> Dict[str, Any]:
        out = {}
        for k, v in asdict(self).items():  # type: ignore[arg-type]
            out[k] = v.value if isinstance(v, Enum) else v
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in d.items() if k in names})  # type: ignore[call-arg]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


@dataclass
class Task(Record):
    request: str
    worker: str
    id: str = field(default_factory=lambda: new_id("task"))
    intent: str = ""
    created: float = field(default_factory=now)
    origin: str = "cli"          # cli | schedule | api
    trigger: str = "manual"      # manual | schedule | api
    procedure: str = ""          # set when the task came from a procedure
    inputs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan(Record):
    run_id: str
    id: str = field(default_factory=lambda: new_id("plan"))
    task_id: str = ""
    intent: str = ""
    rationale: str = ""
    step_ids: List[str] = field(default_factory=list)
    created: float = field(default_factory=now)
    source: str = "model"        # model | procedure | fallback


@dataclass
class Step(Record):
    run_id: str
    plan_id: str
    index: int
    description: str
    id: str = field(default_factory=lambda: new_id("step"))
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    note: str = ""
    observation_id: str = ""
    detail: str = ""


@dataclass
class Action(Record):
    """A tool invocation the Worker WANTS to make. Existence != execution."""

    run_id: str
    step_id: str
    tool: str
    args: Dict[str, Any]
    risk: RiskLevel
    id: str = field(default_factory=lambda: new_id("act"))
    status: ActionStatus = ActionStatus.PROPOSED
    summary: str = ""
    rationale: str = ""
    reason: str = ""
    reversible: bool = True
    approval_id: str = ""
    observation_id: str = ""
    created: float = field(default_factory=now)
    executed: float = 0.0
    attempt: int = 1


@dataclass
class Observation(Record):
    """The literal, untouched result of an action. Never summarised in place —
    a summary is an inference and belongs in a Claim."""

    run_id: str
    action_id: str
    ok: bool
    id: str = field(default_factory=lambda: new_id("obs"))
    output: str = ""
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0
    # §44 — if the ingested content (output/data) was flagged as a suspected
    # prompt-injection attempt, this holds the fired rule name. Empty = benign
    # (or not scanned). Fail-closed: a hit is recorded, never silently trusted.
    injection: str = ""
    created: float = field(default_factory=now)


@dataclass
class Evidence(Record):
    """A pointer to something the Worker actually saw. Evidence is only ever
    created from a real Observation or a real compiled-knowledge record —
    never from model output. See engine._evidence_from_observation."""

    run_id: str
    provenance: Provenance
    summary: str
    id: str = field(default_factory=lambda: new_id("ev"))
    source_ref: str = ""         # file path, atlas claim id, url, tool name
    observation_id: str = ""
    excerpt: str = ""
    created: float = field(default_factory=now)


@dataclass
class Claim(Record):
    run_id: str
    text: str
    id: str = field(default_factory=lambda: new_id("claim"))
    provenance: Provenance = Provenance.HYPOTHESIZED
    confidence: Confidence = Confidence.UNKNOWN
    evidence_ids: List[str] = field(default_factory=list)
    verification_ids: List[str] = field(default_factory=list)
    refuted: bool = False
    created: float = field(default_factory=now)


@dataclass
class Verification(Record):
    """A DETERMINISTIC re-check. No model is involved in deciding PASS/FAIL."""

    run_id: str
    claim_id: str
    check: str
    outcome: VerificationOutcome
    id: str = field(default_factory=lambda: new_id("ver"))
    detail: str = ""
    expected: str = ""
    actual: str = ""
    created: float = field(default_factory=now)


@dataclass
class Approval(Record):
    run_id: str
    action_id: str
    risk: RiskLevel
    summary: str
    id: str = field(default_factory=lambda: new_id("appr"))
    state: ApprovalState = ApprovalState.PENDING
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    decided_by: str = ""
    decided_at: float = 0.0
    note: str = ""
    created: float = field(default_factory=now)
    # §45 HITL escalation / quorum
    quorum: int = 1               # distinct approvers required before it settles
    min_role: str = ""            # minimum RBAC role required to cast a vote ("")
    votes: List[Dict[str, Any]] = field(default_factory=list)  # [{state,by,role,note,at}]
    escalations: int = 0          # how many times the requirement was raised


@dataclass
class Artifact(Record):
    run_id: str
    path: str
    kind: str                    # markdown | csv | json | png | code | message
    id: str = field(default_factory=lambda: new_id("art"))
    title: str = ""
    description: str = ""
    bytes: int = 0
    sha256: str = ""
    claim_ids: List[str] = field(default_factory=list)  # claims this artifact surfaces
    created: float = field(default_factory=now)


@dataclass
class Run(Record):
    task_id: str
    worker: str
    id: str = field(default_factory=lambda: new_id("run"))
    status: RunStatus = RunStatus.PENDING
    plan_id: str = ""
    intent: str = ""
    trigger: str = "manual"
    procedure: str = ""
    started: float = field(default_factory=now)
    finished: float = 0.0
    summary: str = ""
    error: str = ""
    evidence_count: int = 0
    claim_count: int = 0
    approval_count: int = 0
    artifact_ids: List[str] = field(default_factory=list)
    verifications: List[Dict[str, Any]] = field(default_factory=list)
    seq: int = 0                 # human-facing monotonic number
    degradations: List[str] = field(default_factory=list)  # §61: surfaced capability losses
