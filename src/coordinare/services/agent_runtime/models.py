"""Internal contracts for independent, evidence-bound agent phases."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nare import Tool

    from coordinare.services.environment_attempt import AttemptBudget

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class AdmissionIssue:
    code: str
    message: str
    location: tuple[str | int, ...] = ()
    guidance: str | None = None


type AgentCheck = Callable[[JsonObject], Awaitable[list[AdmissionIssue]]]


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
    allowed_tools: tuple[Tool, ...] = ()
    checkpoint_key: str | None = None


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
