"""Trusted environment attempt failures, shared resource budgets and safe diagnostics."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


MAX_ATTEMPT_SECONDS = 3600
MAX_REPAIRS = 2


class EnvironmentDiscoveryError(RuntimeError):
    """Discovery cannot establish an accepted, evidence-bound environment."""

    def __init__(self, message: str, *, code: str = "environment_discovery_held") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DiscoveryChatReply:
    text: str
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None


@dataclass
class AttemptBudget:
    """One monotonic budget across inspection, malformed proposals and fresh reviews."""

    timeout_seconds: float = 600
    max_calls: int = 128
    max_reads: int = 512
    max_repairs: int = 2
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    calls_completed: int = 0
    reads: int = 0
    repairs: int = 0
    inspected_bytes: int = 0
    context_bytes: int = 0
    stage: str = "inventory"
    outcome: str = "held"
    error_code: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    finish_reasons: dict[str, int] = field(default_factory=dict)
    metadata_available: bool = False
    validation_codes: list[str] = field(default_factory=list)
    checkpoint: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (not 0 < self.timeout_seconds <= MAX_ATTEMPT_SECONDS or self.max_calls < 1 or self.max_reads < 1
                or not 0 <= self.max_repairs <= MAX_REPAIRS):
            message = "environment discovery budget limits are invalid"
            raise ValueError(message)

    @property
    def deadline(self) -> float:
        return self.started + self.timeout_seconds

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            message = "environment discovery budget deadline exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_deadline")

    def reserve_call(self) -> None:
        self.check()
        if self.calls >= self.max_calls:
            message = "environment discovery budget model call limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_call_limit")
        self.calls += 1

    def reserve_reads(self, count: int) -> None:
        self.check()
        if type(count) is not int or count < 1 or self.reads + count > self.max_reads:
            message = "environment discovery budget file read limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_read_limit")
        self.reads += count

    def reserve_repair(self) -> None:
        self.check()
        if self.repairs >= self.max_repairs:
            message = "environment discovery budget repair limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_repair_limit")
        self.repairs += 1

    async def flush(self) -> None:
        """Persist reservations before calling a model, source reader or executor."""
        self.check()
        if self.checkpoint is not None:
            await self.checkpoint()
        self.check()

    def record_reply(self, reply: str | DiscoveryChatReply) -> str:
        self.calls_completed += 1
        if isinstance(reply, str):
            return reply
        if not isinstance(reply, DiscoveryChatReply):
            message = "model reply has unsupported metadata"
            raise EnvironmentDiscoveryError(message, code="model_reply_invalid")
        self.metadata_available |= reply.usage is not None or reply.finish_reason is not None
        for key, value in (reply.usage or {}).items():
            if (key in {"input", "output", "input_tokens", "output_tokens", "cache_read", "cache_write"}
                    and type(value) is int and 0 <= value <= 10**12):
                self.usage[key] = self.usage.get(key, 0) + value
        if reply.finish_reason in {"stop", "length", "tool_calls", "content_filter", "function_call"}:
            self.finish_reasons[reply.finish_reason] = self.finish_reasons.get(reply.finish_reason, 0) + 1
        return reply.text

    def summary(self) -> dict[str, Any]:
        """Only trusted stages/codes, counters and allowlisted provider metadata escape."""
        return {"stage": self.stage, "outcome": self.outcome, "error_code": self.error_code,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "calls": self.calls, "calls_completed": self.calls_completed, "reads": self.reads,
                "repairs": self.repairs, "inspected_bytes": self.inspected_bytes,
                "context_bytes": self.context_bytes, "usage": dict(self.usage),
                "finish_reasons": dict(self.finish_reasons), "metadata_available": self.metadata_available,
                "validation_codes": list(self.validation_codes)}
