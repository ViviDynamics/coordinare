"""Per-step token budgets, truncation retry and the per-run call ceiling.

Spec 164 FR-009.  Every rule here comes from a failure measured against the
live gateway during design, not from theory:

* A judgment call truncated mid-JSON at ``max_tokens=500`` and completed
  cleanly at 3000; reasoning consumed the difference.  Hence the per-step
  floors.
* The truncated call returned EMPTY content with ``finish_reason="length"``.
  That is coordinare #244: a truncation reaching the caller as empty content and
  being misread as malformed output.  ``TruncatedResponse`` exists so the two
  causes stay separable, and the retry keys on ``finish_reason`` rather than on
  a parse failure.
* Reasoning models can put the payload in ``reasoning_content`` and leave
  ``content`` empty.  The 073 shim promotes it; the direct LiteLLM path does
  not, and the toolkit calls LiteLLM directly.  So promotion happens here.
* First live run (website #162, glm-5.3-flash, 2026-09-06): the plan call at
  3000 spent the whole budget on 13,391 characters of reasoning, finished with
  ``length`` and no JSON, in 173 s. The doubled retry then outran a 300 s HTTP
  read timeout. A plan-sized prompt with thinking on needs roughly 3,500
  tokens of reasoning before the answer starts, so plan and judge floors are
  8000 and the model caller waits 900 s (see adapter._MODEL_READ_TIMEOUT_S).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

from performer.workflows.base import (
    ModelCallCeilingExceeded,
    TruncatedResponse,
    WorkflowMetrics,
)

log = structlog.get_logger(__name__)

#: Per-step minimum budgets.  Anchored to the measurement above.
_STEP_BUDGETS: dict[str, int] = {
    "plan": 8000,
    "baseline": 0,  # no model call
    "execute": 0,   # no model call
    "observe": 3000,
    "judge": 8000,
    "report": 0,    # no model call
    # 165 architect workflow: the survey proposal is a short list; the
    # blueprint is plan-sized, so it takes the plan floor.
    "survey": 3000,
    "blueprint": 8000,
}
_DEFAULT_BUDGET = 8000

#: Model calls permitted in a single workflow run (plan.md performance budget).
DEFAULT_CALL_LIMIT = 12


@dataclass(frozen=True)
class Budget:
    """The token allowance for one model call."""

    max_tokens: int

    def doubled(self) -> "Budget":
        return Budget(max_tokens=self.max_tokens * 2)

    @classmethod
    def for_step(cls, step: str) -> "Budget":
        return cls(max_tokens=_STEP_BUDGETS.get(step, _DEFAULT_BUDGET) or _DEFAULT_BUDGET)


@dataclass
class ModelReply:
    """The parts of a completion this layer cares about."""

    content: str
    finish_reason: str | None = None
    reasoning_content: str | None = None


class CallCeiling:
    """Bounds model calls per run so a looping workflow fails loudly.

    A single self-hosted model serves every role; an unbounded workflow is a
    denial of service on the gateway, not merely an expensive run.
    """

    def __init__(self, limit: int = DEFAULT_CALL_LIMIT) -> None:
        self.limit = limit
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise ModelCallCeilingExceeded(
                f"workflow exceeded its model-call ceiling of {self.limit}"
            )
        self.used += 1


def _usable_content(reply: ModelReply) -> str:
    content = (reply.content or "").strip()
    if content:
        return content
    # Reasoning models may leave content empty and put the payload in
    # reasoning_content.  Promote it rather than reporting nothing.
    return (reply.reasoning_content or "").strip()


async def call_with_budget(
    call: Callable[[int], Awaitable[ModelReply]],
    budget: Budget,
    metrics: WorkflowMetrics,
) -> ModelReply:
    """Invoke *call*, retrying once at double budget on a truncation.

    Raises ``TruncatedResponse`` when the response is still unusable after the
    retry.  Never raises a parse error for a truncation — that conflation is
    exactly coordinare #244.
    """
    reply = await call(budget.max_tokens)
    metrics.model_calls += 1
    content = _usable_content(reply)
    if content and not (reply.content or "").strip():
        # Round-two review: promotion happened silently. An operator debugging
        # odd output needs to know the payload came from reasoning_content.
        log.info("budget.reasoning_content_promoted", chars=len(content))

    if content and reply.finish_reason != "length":
        return ModelReply(
            content=content,
            finish_reason=reply.finish_reason,
            reasoning_content=reply.reasoning_content,
        )

    # Either truncated, or empty with nothing to promote.  One retry, doubled.
    retry_budget = budget.doubled()
    metrics.truncation_retries += 1
    log.warning(
        "budget.truncation_retry",
        finish_reason=reply.finish_reason,
        first_budget=budget.max_tokens,
        retry_budget=retry_budget.max_tokens,
        promoted_chars=len(content or ""),
    )
    retried = await call(retry_budget.max_tokens)
    metrics.model_calls += 1
    retried_content = _usable_content(retried)

    if retried_content and retried.finish_reason != "length":
        return ModelReply(
            content=retried_content,
            finish_reason=retried.finish_reason,
            reasoning_content=retried.reasoning_content,
        )

    raise TruncatedResponse(
        "model response unusable after retry: "
        f"finish_reason={retried.finish_reason!r} at max_tokens="
        f"{retry_budget.max_tokens} (first attempt: "
        f"finish_reason={reply.finish_reason!r} at {budget.max_tokens})"
    )
