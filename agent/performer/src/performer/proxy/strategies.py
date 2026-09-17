"""080 — orchestration strategies (the planner/executor core).

Each strategy composes three shared steps — ``think`` (planner, tools hidden),
``act`` (executor, plan injected as a system message, tools enabled), and the
final response — behind one ``OrchestrationStrategy`` protocol. Strategies are
pure orchestration over the ``Upstream`` protocol, so they unit-test with fakes.

- ``SingleStrategy``        — one model, no proxy (documented; the proxy is not
  launched for ``single``, but the class keeps the surface complete).
- ``AlwaysThinkThenAct``    — think → act every turn.
- ``ConditionalEscalation`` — classifier-gated think → act, else act-only.
- ``ThinkOnceActMany``      — think once per stage, reuse the cached plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from performer.proxy.classifier import DifficultyClassifier
from performer.proxy.llm_turn import LLMRequest, LLMResponse
from performer.proxy.upstreams import Upstream, UpstreamError

# Default error-marker regex (mirrors coordinare's DEFAULT_THINK_ONCE_ERROR_PATTERN).
DEFAULT_ERROR_PATTERN = r"(?i)\b(error|exception|traceback|fatal|exit code [1-9])\b"

_PLAN_PREFIX = "Follow this plan produced by the planner model:\n\n"


@dataclass
class OrchestrationRecord:
    """Per-turn observability (FR-021) — no secrets, no full bodies."""

    strategy: str
    decision: str = ""  # e.g. "think_then_act", "act_only", "escalated", "cached_plan"
    classifier_score: float | None = None
    plan: str | None = None
    calls: list[dict] = field(default_factory=list)  # [{phase, ok, latency_ms?}]

    def record_call(self, phase: str, ok: bool, latency_ms: float | None = None) -> None:
        entry = {"phase": phase, "ok": ok}
        if latency_ms is not None:
            entry["latency_ms"] = round(latency_ms, 1)
        self.calls.append(entry)


# --- shared steps ----------------------------------------------------------


async def think(thinking: Upstream, request: LLMRequest, rec: OrchestrationRecord) -> str:
    """Planner phase: call the thinking upstream with tools hidden; return plan text."""
    resp = await thinking.complete(request.without_tools(), tools_enabled=False)
    rec.record_call("think", ok=True)
    return (resp.reasoning or resp.content or "").strip()


async def act(tool: Upstream, request: LLMRequest, plan: str | None, rec: OrchestrationRecord) -> LLMResponse:
    """Executor phase: inject the plan as a system message, call the tool upstream."""
    req = request.with_system_prepended(_PLAN_PREFIX + plan) if plan else request
    resp = await tool.complete(req, tools_enabled=True)
    rec.record_call("act", ok=True)
    # carry the plan as reasoning so the assembler can surface it per expose_plan_as
    return LLMResponse(
        content=resp.content,
        tool_calls=resp.tool_calls,
        reasoning=plan or resp.reasoning,
        raw=resp.raw,
    )


# --- strategy protocol -----------------------------------------------------


class OrchestrationStrategy(Protocol):
    name: str

    async def run(self, request: LLMRequest) -> tuple[LLMResponse, OrchestrationRecord]:
        ...


@dataclass
class SingleStrategy:
    """One model, no orchestration. The proxy is not launched for `single`; this
    exists so the strategy surface is complete and uniformly testable."""

    tool: Upstream
    name: str = "single"

    async def run(self, request: LLMRequest) -> tuple[LLMResponse, OrchestrationRecord]:
        rec = OrchestrationRecord(strategy="single", decision="passthrough")
        resp = await self.tool.complete(request, tools_enabled=True)
        rec.record_call("act", ok=True)
        return resp, rec


@dataclass
class AlwaysThinkThenAct:
    """Think (tools hidden) then act (plan injected) on every turn."""

    thinking: Upstream
    tool: Upstream
    on_think_error: str = "fall_back_to_act"  # | "fail"
    name: str = "always"

    async def run(self, request: LLMRequest) -> tuple[LLMResponse, OrchestrationRecord]:
        rec = OrchestrationRecord(strategy="always", decision="think_then_act")
        plan = await _safe_think(self.thinking, request, rec, self.on_think_error)
        resp = await act(self.tool, request, plan, rec)
        rec.plan = plan
        return resp, rec


@dataclass
class ConditionalEscalation:
    """Classifier-gated: score ≥ threshold → think→act, else act-only.

    A classifier failure defaults to think (conservative for correctness)."""

    thinking: Upstream
    tool: Upstream
    classifier: DifficultyClassifier
    threshold: float = 0.6
    on_think_error: str = "fall_back_to_act"
    name: str = "conditional"

    async def run(self, request: LLMRequest) -> tuple[LLMResponse, OrchestrationRecord]:
        rec = OrchestrationRecord(strategy="conditional")
        score = await self.classifier.score(request)  # never raises; failure → 1.0
        rec.classifier_score = score
        if score >= self.threshold:
            rec.decision = "escalated"
            plan = await _safe_think(self.thinking, request, rec, self.on_think_error)
            resp = await act(self.tool, request, plan, rec)
            rec.plan = plan
            return resp, rec
        rec.decision = "act_only"
        resp = await act(self.tool, request, None, rec)
        return resp, rec


@dataclass
class StageState:
    """think_once per-stage (one proxy process) plan cache + invalidation."""

    cached_plan: str | None = None
    turns_since_plan: int = 0
    invalidate_after_turns: int | None = None
    invalidate_on_error: bool = False
    error_pattern: str = DEFAULT_ERROR_PATTERN

    def needs_replan(self, request: LLMRequest) -> bool:
        if self.cached_plan is None:
            return True
        if self.invalidate_after_turns is not None and self.turns_since_plan >= self.invalidate_after_turns:
            return True
        if self.invalidate_on_error and _last_tool_result_is_error(request, self.error_pattern):
            return True
        return False


@dataclass
class ThinkOnceActMany:
    """Think once per stage; reuse the cached plan until invalidated."""

    thinking: Upstream
    tool: Upstream
    state: StageState
    on_think_error: str = "fall_back_to_act"
    name: str = "think_once"

    async def run(self, request: LLMRequest) -> tuple[LLMResponse, OrchestrationRecord]:
        rec = OrchestrationRecord(strategy="think_once")
        if self.state.needs_replan(request):
            plan = await _safe_think(self.thinking, request, rec, self.on_think_error)
            self.state.cached_plan = plan
            self.state.turns_since_plan = 0
            rec.decision = "replanned"
        else:
            rec.decision = "cached_plan"
        self.state.turns_since_plan += 1
        plan = self.state.cached_plan
        resp = await act(self.tool, request, plan, rec)
        rec.plan = plan
        return resp, rec


# --- helpers ---------------------------------------------------------------


async def _safe_think(
    thinking: Upstream, request: LLMRequest, rec: OrchestrationRecord, on_think_error: str,
) -> str | None:
    try:
        return await think(thinking, request, rec)
    except UpstreamError:
        rec.record_call("think", ok=False)
        if on_think_error == "fail":
            raise
        rec.decision = (rec.decision or "") + "+think_fallback"
        return None  # degrade to act-only


def _last_tool_result_is_error(request: LLMRequest, pattern: str) -> bool:
    """FR-016 error marker: the latest tool result carries failure.

    Either an explicit error flag (set by the wire adapter) or a content match
    against the configured pattern. Only tool-role messages are scanned.
    """
    last = request.last_message
    if last is None or last.role != "tool":
        return False
    if last.is_error_tool_result:
        return True
    return re.search(pattern, last.content or "") is not None
