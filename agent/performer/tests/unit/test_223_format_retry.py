from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from structlog.testing import capture_logs

from performer.backends.base import BackendStatus
from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.codex import CodexBackend
from performer.backends.driver import DriverBackend
from performer.backends.opencode import OpenCodeAdapter
from performer.backends.opencode_compat import OpenCodeCompatAdapter
from performer.main import Performance, _handle_backend_parse_failure
from performer.models import Score, Stand

HEAD_CUT = 'sufficient": true, "questions": [], "dependencies": []}'
RETRIES = 2
PORT = 19223
MAX_TOKENS = 1024
TEMPERATURE = 0.2
FAILURE_REASON = "could not be parsed as a JSON object"
JSON_ROLES = (
    "assessing", "assessor", "reviewing", "reviewer", "closing_review", "closer",
    "security", "qa", "documenting", "tech_writer",
)
SHAPES = (
    (HEAD_CUT, "head_cut"),
    ('{"sufficient": true', "tail_cut"),
    ('{"sufficient": nope}', "whole"),
    ('prefix {broken} suffix', "wrapped"),
    ('[{"sufficient": true}]', "array"),
    ("Please clarify the task", "no_object"),
    ("", "empty"),
)


def _performance(tmp_path, backend, role="assessing") -> Performance:
    return Performance(
        session_id="performer-session",
        stand=Stand(path=tmp_path, branch="223-work", git_env={}),
        score=Score(
            title="Assess flatten",
            description="Add flatten to the sample project",
            repo_url="https://github.com/acme/sample",
            branch="223-work",
            role=role,
            model="test-model",
            effort="high",
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        ),
        backend=backend,
        role=role,
    )


async def _retry(perf, raw=HEAD_CUT):
    return await _handle_backend_parse_failure(
        perf, raw, "assessment", SimpleNamespace(BACKEND_PARSE_RETRIES=RETRIES), FAILURE_REASON,
    )


def _backend() -> MagicMock:
    return MagicMock(start=AsyncMock(), stop=AsyncMock(), relay_feedback=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("role", JSON_ROLES)
@pytest.mark.parametrize(("raw", "shape"), SHAPES)
async def test_json_retry_uses_fresh_session_and_shape_specific_repair(tmp_path, role, raw, shape):
    backend = _backend()
    perf = _performance(tmp_path, backend, role)
    with capture_logs() as events:
        response = await _retry(perf, raw)

    assert response.status == "working"
    assert response.session_id == perf.session_id
    assert "fresh session" in response.progress
    assert perf.parse_retry_count == 1
    assert backend.mock_calls == [
        call.stop(),
        call.start(
            perf.stand, perf.score, model=perf.score.model, effort=perf.score.effort,
            temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
        ),
    ]
    repair, = perf.score.relay_feedback
    assert repair["kind"] == "format_repair"
    assert repair["author_login"] == "performer"
    assert "Return ONLY a valid JSON object" in repair["body"]
    if shape == "head_cut":
        assert raw not in repair["body"]
        assert "Previous output" not in repair["body"]
        assert "Emit the complete object from its first character" in repair["body"]
    else:
        assert f"Previous output preview: {raw or '<empty>'}" in repair["body"]
    retry_event, = [e for e in events if e["event"] == "backend.invalid_output.retrying"]
    assert retry_event["fresh_session"] is True
    assert retry_event["output_shape"] == shape
    assert retry_event["output_preview"] == raw
    assert retry_event["attempt"] == 1
    assert retry_event["max_retries"] == RETRIES
    raw_event, = [e for e in events if e["event"] == "backend.parse_failure.raw_shape"]
    assert raw_event["length"] == len(raw)


@pytest.mark.asyncio
async def test_retry_budget_keeps_capture_and_only_latest_repair(tmp_path):
    backend = _backend()
    perf = _performance(tmp_path, backend)
    human_feedback = {"body": "Keep the acceptance criteria", "author_login": "octocat"}
    perf.score.relay_feedback = [
        human_feedback, {"kind": "format_repair", "body": "Previous output preview: damaged"},
    ]
    for raw in ("an earlier invalid response", HEAD_CUT):
        assert (await _retry(perf, raw)).status == "working"
    assert len(perf.score.relay_feedback) == RETRIES
    assert perf.score.relay_feedback[0] == human_feedback
    assert "damaged" not in perf.score.relay_feedback[-1]["body"]
    assert "an earlier invalid response" not in perf.score.relay_feedback[-1]["body"]
    response = await _retry(perf)
    assert response.status == "error"
    assert response.reason.startswith("BACKEND_FORMAT_ERROR:")
    assert f"after {RETRIES + 1} attempts" in response.reason
    assert f"Output length {len(HEAD_CUT)} chars, shape head_cut, capture whole" in response.reason
    assert HEAD_CUT in response.reason
    assert perf.parse_retry_count == RETRIES
    assert backend.start.await_count == RETRIES
    assert backend.stop.await_count == RETRIES
    backend.relay_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_failure_still_starts_fresh_session(tmp_path):
    backend = _backend()
    backend.stop.side_effect = RuntimeError("already stopped")
    perf = _performance(tmp_path, backend)
    with capture_logs() as events:
        assert (await _retry(perf)).status == "working"
    backend.start.assert_awaited_once()
    backend.relay_feedback.assert_not_awaited()
    assert any(e["event"] == "backend.parse_retry_stop_failed" for e in events)


def _assert_prompt(prompt):
    assert "Assess flatten" in prompt
    assert "Add flatten to the sample project" in prompt
    assert "## Output Format Correction" in prompt
    assert "Return ONLY a valid JSON object" in prompt
    assert HEAD_CUT not in prompt
    assert "Previous output" not in prompt


@pytest.mark.asyncio
async def test_claude_retry_does_not_pass_resume_session(tmp_path, monkeypatch):
    backend = ClaudeCodeBackend()
    backend._session_id = "damaged-session"
    backend._status = BackendStatus(state="done", output=HEAD_CUT)
    launch = AsyncMock()
    monkeypatch.setattr(backend, "_launch", launch)
    monkeypatch.setattr(backend, "stop", AsyncMock())
    relay = AsyncMock()
    monkeypatch.setattr(backend, "relay_feedback", relay)
    assert (await _retry(_performance(tmp_path, backend))).status == "working"
    launch.assert_awaited_once()
    prompt = launch.await_args.args[0]
    launch.assert_awaited_once_with(prompt)
    _assert_prompt(prompt)
    assert backend.get_status().state == "working"
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_driver_retry_allocates_new_session_file(tmp_path, monkeypatch):
    backend = DriverBackend()
    old_session = tmp_path / "damaged-session.json"
    backend._session_path = old_session
    backend._status = BackendStatus(state="done", output=HEAD_CUT)
    spawn = AsyncMock()
    monkeypatch.setattr(backend, "_spawn", spawn)
    monkeypatch.setattr(backend, "stop", AsyncMock())
    assert (await _retry(_performance(tmp_path, backend))).status == "working"
    command = spawn.await_args.args[0]
    assert "--resume" not in command
    assert "--session" in command
    assert backend._session_path != old_session
    assert command[command.index("--session") + 1] == str(backend._session_path)
    _assert_prompt(command[-1])
    assert backend.get_status().state == "working"


@pytest.mark.asyncio
async def test_codex_retry_starts_new_thread(tmp_path, monkeypatch):
    backend = CodexBackend()
    backend._thread_id = "damaged-thread"
    backend._status = BackendStatus(state="done", output=HEAD_CUT)
    monkeypatch.setattr("performer.backends.codex._find_free_port", lambda: PORT)
    monkeypatch.setattr("performer.backends.codex.asyncio.create_subprocess_exec", AsyncMock())
    client = MagicMock(ws_connect=AsyncMock())
    monkeypatch.setattr("performer.backends.codex.aiohttp.ClientSession", lambda: client)
    for method in ("_wait_for_ready", "_drain_logs", "_recv_loop", "_notify", "stop"):
        monkeypatch.setattr(backend, method, AsyncMock())
    rpc = AsyncMock(side_effect=[{}, {"thread": {"id": "fresh-thread"}}, {"turn": {"id": "fresh-turn"}}])
    monkeypatch.setattr(backend, "_rpc", rpc)
    try:
        assert (await _retry(_performance(tmp_path, backend))).status == "working"
        assert [c.args[0] for c in rpc.await_args_list] == ["initialize", "thread/start", "turn/start"]
        turn = rpc.await_args_list[-1].args[1]
        assert turn["threadId"] == "fresh-thread"
        _assert_prompt(turn["input"][0]["text"])
        assert backend.get_status().state == "working"
    finally:
        await backend._log_drain_task
        await backend._recv_task


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [OpenCodeAdapter, OpenCodeCompatAdapter])
async def test_opencode_retry_creates_new_session(tmp_path, monkeypatch, factory):
    backend = factory()
    backend._session_id = "damaged-session"
    backend._status = BackendStatus(state="done", output=HEAD_CUT)
    module = factory.__module__
    monkeypatch.setattr(f"{module}._find_free_port", lambda: PORT)
    monkeypatch.setattr(f"{module}.asyncio.create_subprocess_exec", AsyncMock())
    response = MagicMock()
    response.json.return_value = {"id": "fresh-session"}
    client = MagicMock(post=AsyncMock(return_value=response))
    monkeypatch.setattr(f"{module}.httpx.AsyncClient", lambda **kwargs: client)
    for method in ("_wait_for_ready", "_drain_logs", "_event_reader_loop", "stop"):
        monkeypatch.setattr(backend, method, AsyncMock())
    try:
        assert (await _retry(_performance(tmp_path, backend))).status == "working"
        session, prompt = client.post.await_args_list
        assert session.args[0] == "/session"
        assert prompt.args[0] == "/session/fresh-session/prompt_async"
        _assert_prompt(prompt.kwargs["json"]["parts"][0]["text"])
        assert backend.get_status().state == "working"
    finally:
        await backend._log_drain_task
        await backend._reader_task
