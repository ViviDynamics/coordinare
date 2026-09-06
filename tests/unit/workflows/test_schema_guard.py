"""T009 — FR-010 / FR-017: schema validation with exactly one reprompt.

Measured during design: the description step emitted element kinds outside the
enum it was given (`text:`, and a `text_input` with an empty label).  A step
that silently accepts out-of-schema output degrades quietly, which is the
failure mode spec 120 exists to prevent.
"""
from __future__ import annotations

import pytest
from performer.workflows.base import SchemaViolation, WorkflowMetrics
from performer.workflows.schema_guard import extract_json, validate_with_reprompt
from pydantic import BaseModel


class Reply(BaseModel):
    ok: bool
    count: int


def _yielding(*payloads):
    seen: list[str | None] = []
    it = iter(payloads)

    async def _call(reprompt: str | None) -> str:
        seen.append(reprompt)
        return next(it)

    return _call, seen


@pytest.mark.asyncio
async def test_valid_output_parses_without_a_reprompt():
    caller, seen = _yielding('{"ok": true, "count": 2}')
    metrics = WorkflowMetrics()
    result = await validate_with_reprompt(caller, Reply, metrics)

    assert result.ok is True and result.count == 2
    assert seen == [None], "a valid first response must not trigger a reprompt"
    assert metrics.schema_reprompts == 0


@pytest.mark.asyncio
async def test_one_violation_reprompts_once_then_succeeds():
    caller, seen = _yielding('{"ok": "yes", "count": "many"}', '{"ok": true, "count": 1}')
    metrics = WorkflowMetrics()
    result = await validate_with_reprompt(caller, Reply, metrics)

    assert result.count == 1
    assert len(seen) == 2
    assert seen[1] is not None, "the reprompt must tell the model what was wrong"
    assert metrics.schema_reprompts == 1


@pytest.mark.asyncio
async def test_two_violations_raise_rather_than_degrading():
    caller, seen = _yielding('{"ok": "no"}', '{"still": "wrong"}')
    with pytest.raises(SchemaViolation):
        await validate_with_reprompt(caller, Reply, WorkflowMetrics())
    assert len(seen) == 2, "exactly one reprompt, then fail loudly"


@pytest.mark.asyncio
async def test_reprompt_names_the_offending_fields():
    # NB "yes"/"true"/"on" ARE valid bools under pydantic's lax coercion, so a
    # genuinely invalid value is needed here or nothing reprompts.
    caller, seen = _yielding('{"ok": "definitely", "count": 1}', '{"ok": true, "count": 1}')
    await validate_with_reprompt(caller, Reply, WorkflowMetrics())
    assert "ok" in seen[1]


def test_extract_json_handles_prose_wrapped_output():
    """Models wrap JSON in commentary or fences; that is not a schema violation."""
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nHope that helps') == '{"a": 1}'
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_returns_none_when_there_is_no_object():
    assert extract_json("I could not complete this task.") is None
    assert extract_json("") is None


@pytest.mark.asyncio
async def test_a_schema_violation_carries_an_excerpt_of_what_the_model_said():
    """Round-two review: SchemaViolation said only 'no JSON object after
    reprompt'. Whether the model returned nothing, prose, or a near-miss is the
    whole diagnosis, and it was thrown away."""
    caller, _ = _yielding("I cannot help with that request.", "Still refusing, sorry.")
    with pytest.raises(SchemaViolation) as exc:
        await validate_with_reprompt(caller, Reply, WorkflowMetrics())
    msg = str(exc.value)
    assert "Still refusing" in msg, "the retried response must be quoted"
    assert "I cannot help" in msg, "and the first one"


@pytest.mark.asyncio
async def test_the_excerpt_is_length_capped():
    """Spec 083 FR-011 forbids logging raw model text wholesale; an excerpt is
    diagnostic, a transcript is a leak."""
    huge = "x" * 5000
    caller, _ = _yielding(huge, huge)
    with pytest.raises(SchemaViolation) as exc:
        await validate_with_reprompt(caller, Reply, WorkflowMetrics())
    assert len(str(exc.value)) < 1200


@pytest.mark.asyncio
async def test_a_reprompt_is_logged_with_the_problem(monkeypatch):
    import performer.workflows.schema_guard as sg
    from performer.workflows.base import WorkflowMetrics

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(sg, "log", fake)

    class _M(BaseModel):
        ok: bool

    answers = iter(['not json at all', '{"ok": true}'])

    async def call(correction):
        return next(answers)

    result = await validate_with_reprompt(call, _M, WorkflowMetrics())

    assert result.ok is True
    entry = next(e for e in fake.entries if e["event"] == "schema_guard.reprompt")
    assert "no JSON object" in entry["problem"]
