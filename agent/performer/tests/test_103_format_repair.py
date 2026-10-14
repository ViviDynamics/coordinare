"""103: a BACKEND_FORMAT_ERROR retry must change something, and the failure
must say which end of the malformed output was lost."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from performer.main import (
    Performance,
    _handle_backend_parse_failure,
    _json_recovery_prompt,
    _output_shape,
)
from performer.models import Score, Stand

# The exact capture reported in issue #103: the opening of the object is gone,
# the closing brace survives.
_HEAD_CUT = 'ufficient": true, "questions": [], "dependencies": []}'


def _perf(parse_retry_count: int = 0, backend: object | None = None) -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(
        title="Format phone",
        repo_url="https://github.com/acme/repo",
        branch="feat/test",
        model="m",
        max_tokens=100,
    )
    perf = Performance(
        session_id="sid",
        stand=stand,
        score=score,
        backend=backend if backend is not None else MagicMock(),
        role="assessing",
    )
    perf.parse_retry_count = parse_retry_count
    return perf


def _settings(retries: int) -> SimpleNamespace:
    return SimpleNamespace(BACKEND_PARSE_RETRIES=retries)


def test_output_shape_names_a_head_cut_object() -> None:
    assert _output_shape(_HEAD_CUT) == "head_cut"


def test_output_shape_names_a_tail_cut_object() -> None:
    assert _output_shape('{"sufficient": true, "questions": [') == "tail_cut"


def test_output_shape_leaves_a_balanced_object_as_whole() -> None:
    assert _output_shape('{"sufficient": true, "questions": []}') == "whole"


def test_output_shape_reports_empty_output() -> None:
    assert _output_shape("   ") == "empty"


@pytest.mark.asyncio
async def test_exhausted_reason_reports_length_shape_and_whole_capture() -> None:
    perf = _perf(parse_retry_count=0)
    result = await _handle_backend_parse_failure(
        perf,
        _HEAD_CUT,
        "assessment",
        _settings(0),
        "could not be parsed as a JSON object",
    )
    assert result.status == "error"
    reason = perf.error_reason or ""
    assert reason.startswith("BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed")
    assert f"Output length {len(_HEAD_CUT)} chars" in reason
    assert "shape head_cut" in reason
    assert "capture whole" in reason
    assert _HEAD_CUT in reason


@pytest.mark.asyncio
async def test_long_output_reports_head_and_tail_capture() -> None:
    raw = "{" + "a" * 1000 + "TAILMARK" + "}"
    perf = _perf(parse_retry_count=0)
    await _handle_backend_parse_failure(
        perf,
        raw,
        "assessment",
        _settings(0),
        "could not be parsed as a JSON object",
    )
    reason = perf.error_reason or ""
    assert f"Output length {len(raw)} chars" in reason
    assert "capture head 300 and tail 200" in reason
    assert "TAILMARK" in reason
    # The length must precede the excerpt so a daemon-side log cut keeps it.
    assert reason.index("Output length") < reason.index("Last output:")


@pytest.mark.asyncio
async def test_secret_is_redacted_before_the_capture_is_sliced() -> None:
    token = "ghp_" + "A" * 36
    # Place the token so a 300-char head slice would split it.
    raw = "{" + "x" * 290 + token + "}"
    perf = _perf(parse_retry_count=0)
    await _handle_backend_parse_failure(
        perf,
        raw,
        "assessment",
        _settings(0),
        "could not be parsed as a JSON object",
    )
    reason = perf.error_reason or ""
    assert "ghp_" not in reason
    assert "AAAA" not in reason


@pytest.mark.asyncio
async def test_retry_repair_prompt_carries_the_tail_of_the_malformed_output() -> None:
    backend = MagicMock()
    backend.relay_feedback = AsyncMock()
    raw = "{" + "a" * 500 + "ENDMARK" + "}"
    perf = _perf(parse_retry_count=0, backend=backend)
    result = await _handle_backend_parse_failure(
        perf,
        raw,
        "assessment",
        _settings(1),
        "could not be parsed as a JSON object",
    )
    assert result.status == "working"
    backend.relay_feedback.assert_awaited_once()
    prompt = backend.relay_feedback.await_args.args[0]
    assert "Previous output tail:" in prompt
    assert "ENDMARK" in prompt
    assert "could not be parsed" in prompt


@pytest.mark.asyncio
async def test_identical_restart_is_changed_by_a_repair_hint_on_the_score() -> None:
    backend = MagicMock()
    backend.relay_feedback = AsyncMock(side_effect=RuntimeError("no interactive channel"))
    backend.stop = AsyncMock()
    backend.start = AsyncMock()
    perf = _perf(parse_retry_count=0, backend=backend)
    result = await _handle_backend_parse_failure(
        perf,
        _HEAD_CUT,
        "assessment",
        _settings(1),
        "could not be parsed as a JSON object",
    )
    assert result.status == "working"
    backend.start.assert_awaited_once()
    started_score = backend.start.await_args.args[1]
    hints = [item for item in started_score.relay_feedback if isinstance(item, dict)]
    assert hints, "the restarted backend must receive a repair hint"
    body = hints[-1]["body"]
    assert _HEAD_CUT in body
    assert "opening brace" in body


def test_recovery_prompt_names_the_head_cut_and_includes_tail() -> None:
    prompt = _json_recovery_prompt(
        role="assessing",
        stage_label="assessment",
        failure_reason="could not be parsed as a JSON object",
        output_preview="ufficient",
        output_tail="ENDMARK}",
        output_shape="head_cut",
    )
    assert "Previous output tail: ENDMARK}" in prompt
    assert "opening brace" in prompt
