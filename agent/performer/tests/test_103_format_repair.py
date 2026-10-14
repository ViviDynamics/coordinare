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


def _restartable_backend() -> MagicMock:
    backend = MagicMock()
    backend.relay_feedback = AsyncMock()
    backend.stop = AsyncMock()
    backend.start = AsyncMock()
    return backend


@pytest.mark.asyncio
async def test_retry_repair_prompt_carries_the_tail_of_the_malformed_output() -> None:
    backend = _restartable_backend()
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
    backend.start.assert_awaited_once()
    prompt = backend.start.await_args.args[1].relay_feedback[-1]["body"]
    assert "Previous output tail:" in prompt
    assert "ENDMARK" in prompt
    assert "could not be parsed" in prompt


@pytest.mark.asyncio
async def test_format_retry_starts_a_fresh_session_instead_of_resuming() -> None:
    # 223: a resumed retry of a head-cut output failed every time; a fresh one recovered.
    backend = _restartable_backend()
    perf = _perf(parse_retry_count=0, backend=backend)
    result = await _handle_backend_parse_failure(
        perf,
        _HEAD_CUT,
        "assessment",
        _settings(1),
        "could not be parsed as a JSON object",
    )
    assert result.status == "working"
    assert "fresh session" in (result.progress or "")
    backend.relay_feedback.assert_not_awaited()
    backend.stop.assert_awaited_once()
    backend.start.assert_awaited_once()
    started_score = backend.start.await_args.args[1]
    hints = [item for item in started_score.relay_feedback if isinstance(item, dict)]
    assert hints, "the restarted backend must receive a repair hint"
    body = hints[-1]["body"]
    assert "opening brace" in body
    # 223: the fragment lost its head in transport, so it is not quoted back.
    assert _HEAD_CUT not in body
    assert "Previous output" not in body


@pytest.mark.asyncio
async def test_repeated_format_retries_carry_one_repair_hint() -> None:
    backend = _restartable_backend()
    perf = _perf(parse_retry_count=0, backend=backend)
    perf.score.relay_feedback = [
        {"body": "daemon hint", "kind": "format_repair"},
        {"body": "human review", "author_login": "octocat"},
    ]
    for _ in range(2):
        await _handle_backend_parse_failure(
            perf,
            _HEAD_CUT,
            "assessment",
            _settings(2),
            "could not be parsed as a JSON object",
        )
    feedback = perf.score.relay_feedback
    repairs = [item for item in feedback if item.get("kind") == "format_repair"]
    assert len(repairs) == 1
    assert repairs[0]["author_login"] == "performer"
    assert {"body": "human review", "author_login": "octocat"} in feedback


def test_recovery_prompt_names_the_head_cut_without_quoting_it() -> None:
    prompt = _json_recovery_prompt(
        role="assessing",
        stage_label="assessment",
        failure_reason="could not be parsed as a JSON object",
        output_preview="ufficient",
        output_tail="ENDMARK}",
        output_shape="head_cut",
    )
    assert "opening brace" in prompt
    assert "Required top-level keys: sufficient, questions" in prompt
    assert "ufficient\"" not in prompt
    assert "ENDMARK" not in prompt
    assert "Previous output" not in prompt


def test_recovery_prompt_quotes_other_shapes() -> None:
    prompt = _json_recovery_prompt(
        role="assessing",
        stage_label="assessment",
        failure_reason="could not be parsed as a JSON object",
        output_preview="{bad",
        output_tail="ENDMARK",
        output_shape="tail_cut",
    )
    assert "Previous output preview: {bad" in prompt
    assert "Previous output tail: ENDMARK" in prompt
