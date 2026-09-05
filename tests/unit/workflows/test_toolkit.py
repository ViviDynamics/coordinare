"""T012 — the six Toolkit guarantees in contracts/role_workflow.md."""
from __future__ import annotations

import pytest
from performer.workflows.base import (
    ModelCallCeilingExceeded,
    SchemaViolation,
    TruncatedResponse,
    WorkflowMetrics,
)
from performer.workflows.budget import Budget, ModelReply
from performer.workflows.toolkit import Toolkit
from pydantic import BaseModel


class Tiny(BaseModel):
    ok: bool


def _toolkit(*, replies=None, runner=None, capture=None, call_limit=12):
    replies = list(replies or [])
    it = iter(replies)

    async def _model(_persona, _content, max_tokens):
        return next(it)

    return Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_model,
        command_runner=runner,
        screenshot_capture=capture,
        call_limit=call_limit,
    )


# --- Guarantee 1: the call ceiling is enforced, never silently exceeded ---

@pytest.mark.asyncio
async def test_call_model_enforces_the_ceiling():
    tk = _toolkit(replies=[ModelReply('{"ok": true}', "stop")] * 5, call_limit=2)
    await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))
    await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))
    with pytest.raises(ModelCallCeilingExceeded):
        await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))


# --- Guarantee 2: truncation surfaces as TruncatedResponse, not a parse error ---

@pytest.mark.asyncio
async def test_truncation_is_not_reported_as_a_schema_problem():
    tk = _toolkit(replies=[ModelReply("", "length"), ModelReply('{"partial":', "length")])
    with pytest.raises(TruncatedResponse):
        await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(1500))


# --- Guarantee 3: schema violation reprompts once, then raises ---

@pytest.mark.asyncio
async def test_schema_violation_reprompts_once_then_raises():
    tk = _toolkit(replies=[ModelReply('{"ok": "nope"}', "stop"),
                           ModelReply('{"ok": "still"}', "stop")])
    with pytest.raises(SchemaViolation):
        await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))


# --- Guarantee 4: run_command returns a REAL exit code ---

@pytest.mark.asyncio
async def test_run_command_reports_the_real_exit_code():
    async def runner(cmd, cwd, timeout):
        return (7, "boom")

    tk = _toolkit(runner=runner)
    check = await tk.run_command("false", cwd=None)
    assert check.exit_code == 7
    assert check.passed is False
    assert check.command == "false"


@pytest.mark.asyncio
async def test_run_command_never_infers_success_from_output():
    async def runner(cmd, cwd, timeout):
        return (1, "All tests passed!")  # lying output, failing exit code

    tk = _toolkit(runner=runner)
    check = await tk.run_command("pytest", cwd=None)
    assert check.passed is False, "exit code wins over any claim in the output"


# --- Guarantee 6: capture never returns an unverified path ---

@pytest.mark.asyncio
async def test_capture_returns_none_rather_than_an_unverified_path():
    tk = _toolkit(capture=lambda **_: None)
    assert await tk.capture_screenshot(url="http://localhost:1/", path=None) is None


@pytest.mark.asyncio
async def test_capture_returns_the_path_when_the_helper_verified_it(tmp_path):
    real = tmp_path / "shot.png"
    real.write_bytes(b"png")
    tk = _toolkit(capture=lambda **_: str(real))
    assert await tk.capture_screenshot(url="http://localhost:1/", path=None) == str(real)


# --- Metrics are recorded so the performance budgets are measurable ---

@pytest.mark.asyncio
async def test_metrics_track_calls_and_reprompts():
    tk = _toolkit(replies=[ModelReply('{"ok": "nope"}', "stop"),
                           ModelReply('{"ok": true}', "stop")])
    await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))
    assert tk.metrics.model_calls >= 1
    assert tk.metrics.schema_reprompts == 1


def test_get_backend_delegates_to_the_backend_registry():
    tk = _toolkit()
    from performer.backends import UnsupportedBackendError

    with pytest.raises(UnsupportedBackendError):
        tk.get_backend("not-a-backend")


# --- The model must be TOLD the schema it is being validated against ---

@pytest.mark.asyncio
async def test_call_model_gives_the_model_the_output_schema():
    """Found by the first live eval run.

    The model returned a semantically correct plan with invented field names
    (`id` and `kind` missing on every check, `steps` as prose strings) because
    nothing in the request described the expected shape. Validating against a
    schema the model was never shown is not a contract, it is a guessing game.
    """
    seen: list[str] = []

    async def _model(persona, content, max_tokens):
        seen.append(persona)
        return ModelReply('{"ok": true}', "stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_model)
    await tk.call_model(persona="be helpful", schema=Tiny, content=[], budget=Budget(3000))

    assert seen, "the model was never called"
    sent = seen[0]
    assert "be helpful" in sent, "the step persona must survive"
    assert '"ok"' in sent, "the schema's field names must reach the model"
    assert "boolean" in sent.lower(), "field types must reach the model"


@pytest.mark.asyncio
async def test_a_schema_reprompt_repeats_the_schema_not_just_the_complaint():
    """The live failure went: no JSON at all, then a reprompt carrying only
    'response contained no JSON object', then JSON with invented field names.
    The correction must carry the shape, not only the complaint."""
    seen: list[str] = []

    async def _model(persona, content, max_tokens):
        seen.append(persona)
        return ModelReply("I cannot do that." if len(seen) == 1 else '{"ok": true}', "stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_model)
    await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(3000))

    assert len(seen) == 2, "expected one reprompt"
    assert '"ok"' in seen[1], "the reprompt must restate the schema"


@pytest.mark.asyncio
async def test_dom_snapshot_accepts_an_async_reader():
    """Found by the first live eval run.

    dom_snapshot is async but called its reader synchronously, so an async
    reader returned an un-awaited coroutine. A real browser reader has to be
    async: Playwright's sync API refuses to run inside a running event loop,
    which is exactly where a workflow step lives.
    """
    async def _reader(url):
        return [{"kind": "button", "label": "Continue", "url": url}]

    tk = Toolkit(metrics=WorkflowMetrics(), dom_reader=_reader)
    elements = await tk.dom_snapshot("http://127.0.0.1:8000/")

    assert elements[0]["label"] == "Continue"
    assert elements[0]["url"] == "http://127.0.0.1:8000/"


@pytest.mark.asyncio
async def test_dom_snapshot_still_accepts_a_plain_sync_reader():
    tk = Toolkit(metrics=WorkflowMetrics(), dom_reader=lambda _u: [{"kind": "heading"}])
    assert (await tk.dom_snapshot("http://x/"))[0]["kind"] == "heading"


@pytest.mark.asyncio
async def test_the_ceiling_counts_every_real_request_not_every_call_model():
    """Round-two review, critical.

    call_model consumed ONE ceiling unit, but a single call_model can make up
    to four real requests: a truncation retry inside call_with_budget, then a
    schema reprompt that itself may retry. A ceiling of 12 therefore permitted
    up to 48 requests against a gateway shared by every role -- the DoS guard
    the ceiling exists to be, bypassed by the machinery beneath it.
    """
    replies = iter([
        ModelReply("", "length"),                 # 1: truncated
        ModelReply('{"ok": "nope"}', "stop"),      # 2: retry -> schema-invalid
        ModelReply("", "length"),                 # 3: reprompt -> truncated
        ModelReply('{"ok": true}', "stop"),        # 4: retry -> valid
    ])
    calls = 0

    async def _model(_p, _c, _mt):
        nonlocal calls
        calls += 1
        return next(replies)

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_model, call_limit=4)
    await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(1000))
    assert calls == 4, "four real requests were made"
    assert tk._ceiling.used == 4, "and all four must count against the ceiling"


@pytest.mark.asyncio
async def test_a_retry_that_would_exceed_the_ceiling_is_refused():
    replies = iter([ModelReply("", "length"), ModelReply('{"ok": true}', "stop")])

    async def _model(_p, _c, _mt):
        return next(replies)

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_model, call_limit=1)
    with pytest.raises(ModelCallCeilingExceeded):
        await tk.call_model(persona="p", schema=Tiny, content=[], budget=Budget(1000))
