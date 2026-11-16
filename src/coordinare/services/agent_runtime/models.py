"""Internal contracts for independent, evidence-bound agent phases."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nare import Tool

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.agent_runtime.evidence import EvidenceStore
    from coordinare.services.agent_runtime.request_stage import RequestStage
    from coordinare.services.environment_attempt import AttemptBudget

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class AdmissionIssue:
    code: str
    message: str
    location: tuple[str | int, ...] = ()
    guidance: str | None = None


type AgentCheck = Callable[[JsonObject], Awaitable[list[AdmissionIssue]]]
type AgentDiagnose = Callable[[object], Awaitable[list[AdmissionIssue]]]


class AgentToolState(Protocol):
    def snapshot(self) -> JsonObject: ...
    async def restore(self, saved: JsonObject) -> None: ...


@dataclass(frozen=True)
class AgentIdentity:
    attempt_id: str
    phase: str
    role: str
    source_commit: str
    domain_digests: tuple[tuple[str, str], ...]
    policy_digest: str
    prompt_digest: str
    schema_digest: str
    model_config_digest: str


@dataclass(frozen=True)
class AgentRequest:
    identity: AgentIdentity
    instructions: str
    task: str
    answer_schema: JsonObject
    check: AgentCheck
    budget: AttemptBudget
    diagnose: AgentDiagnose | None = None
    allowed_tools: tuple[Tool, ...] = ()
    evidence_tools: frozenset[str] = frozenset()
    checkpoint_key: str | None = None
    checkpoint_store: CheckpointStore | None = None
    evidence_catalog: EvidenceStore | None = None
    event_sink: Callable[[JsonObject], Awaitable[None]] | None = None
    phase_deadline: float | None = None
    phase_max_calls: int | None = None
    max_output_bytes: int | None = None
    max_context_bytes: int | None = None
    shard_corrections: int | None = None
    effects: EffectJournal | None = None
    tool_state: AgentToolState | None = None
    prerequisites: frozenset[str] = frozenset()
    request_stage: RequestStage | None = None


@dataclass(frozen=True)
class AgentResult:
    status: Literal['accepted', 'held', 'cancelled', 'error']
    answer: JsonObject | None = None
    hold_code: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    checkpoint_key: str | None = None
    evidence_receipt: tuple[str, ...] = ()


class AgentRuntime(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...
