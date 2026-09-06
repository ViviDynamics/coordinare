"""T007 — FR-009 / FR-016: token budget, truncation retry, call ceiling.

These encode failures measured live against the gateway during the spec-164
design session, not hypothetical ones:

  * a judgment call truncated mid-JSON at max_tokens=500 and completed cleanly
    at 3000, with reasoning consuming the difference;
  * the truncated call returned EMPTY content, which is coordinare #244 -- a
    truncation that reaches the caller as empty content and gets misread as
    malformed output.
"""
from __future__ import annotations

import pytest
from performer.workflows.base import ModelCallCeilingExceeded, TruncatedResponse, WorkflowMetrics
from performer.workflows.budget import Budget, CallCeiling, ModelReply, call_with_budget


def _replies(*items):
    """Build a fake caller that returns each reply in turn, recording budgets."""
    seen: list[int] = []
    it = iter(items)

    async def _call(max_tokens: int) -> ModelReply:
        seen.append(max_tokens)
        return next(it)

    return _call, seen


@pytest.mark.asyncio
async def test_truncation_retries_once_at_double_budget_and_succeeds():
    caller, seen = _replies(
        ModelReply(content="", finish_reason="length"),
        ModelReply(content='{"ok": true}', finish_reason="stop"),
    )
    metrics = WorkflowMetrics()
    reply = await call_with_budget(caller, Budget(max_tokens=3000), metrics)

    assert reply.content == '{"ok": true}'
    assert seen == [3000, 6000], "retry must double the budget, not repeat it"
    assert metrics.truncation_retries == 1


@pytest.mark.asyncio
async def test_persistent_truncation_raises_truncated_not_a_parse_error():
    """The whole point of #244: a truncation must stay distinguishable from
    malformed output, or coordinare misclassifies the cause."""
    caller, _ = _replies(
        ModelReply(content="", finish_reason="length"),
        ModelReply(content='{"partial": ', finish_reason="length"),
    )
    with pytest.raises(TruncatedResponse) as exc:
        await call_with_budget(caller, Budget(max_tokens=1500), WorkflowMetrics())
    assert "length" in str(exc.value)


@pytest.mark.asyncio
async def test_retry_happens_only_once():
    caller, seen = _replies(
        ModelReply(content="", finish_reason="length"),
        ModelReply(content="", finish_reason="length"),
    )
    with pytest.raises(TruncatedResponse):
        await call_with_budget(caller, Budget(max_tokens=1000), WorkflowMetrics())
    assert len(seen) == 2, "exactly one retry; unbounded retries burn the gateway"


@pytest.mark.asyncio
async def test_empty_content_with_reasoning_is_promoted_not_called_malformed():
    """Measured: the shim promotes reasoning_content, the direct LiteLLM path
    does not.  The toolkit calls LiteLLM directly, so it must promote."""
    caller, _ = _replies(
        ModelReply(content="", finish_reason="stop", reasoning_content='{"ok": 1}')
    )
    reply = await call_with_budget(caller, Budget(max_tokens=3000), WorkflowMetrics())
    assert reply.content == '{"ok": 1}'


@pytest.mark.asyncio
async def test_empty_content_and_no_reasoning_on_a_stop_is_an_error():
    """An empty completion earns one retry like a truncation does, then fails
    as unusable rather than being handed on as valid empty output."""
    caller, seen = _replies(
        ModelReply(content="", finish_reason="stop"),
        ModelReply(content="", finish_reason="stop"),
    )
    with pytest.raises(TruncatedResponse):
        await call_with_budget(caller, Budget(max_tokens=3000), WorkflowMetrics())
    assert seen == [3000, 6000]


def test_call_ceiling_raises_rather_than_continuing_silently():
    ceiling = CallCeiling(limit=3)
    for _ in range(3):
        ceiling.consume()
    with pytest.raises(ModelCallCeilingExceeded) as exc:
        ceiling.consume()
    assert "3" in str(exc.value)


def test_call_ceiling_default_is_the_documented_twelve():
    """plan.md budget: <=12 model calls per QA run."""
    assert CallCeiling().limit == 12


def test_budget_floors_are_the_measured_ones():
    """500 truncated, 3000 completed in design; the first live plan call spent
    3000 entirely on reasoning (13,391 chars, finish=length). Plan and judge
    need >=8000, observation >=3000."""
    assert Budget.for_step("judge").max_tokens >= 8000
    assert Budget.for_step("observe").max_tokens >= 3000
    assert Budget.for_step("plan").max_tokens >= 8000


@pytest.mark.asyncio
async def test_a_truncation_retry_is_logged_with_both_budgets():
    from structlog.testing import capture_logs

    calls: list[int] = []

    async def call(max_tokens: int) -> ModelReply:
        calls.append(max_tokens)
        if len(calls) == 1:
            return ModelReply(content="", finish_reason="length", reasoning_content="thinking...")
        return ModelReply(content='{"ok": true}', finish_reason="stop")

    with capture_logs() as logs:
        await call_with_budget(call, Budget(max_tokens=3000), WorkflowMetrics())

    entry = next(e for e in logs if e["event"] == "budget.truncation_retry")
    assert entry["first_budget"] == 3000 and entry["retry_budget"] == 6000
    assert entry["finish_reason"] == "length"
