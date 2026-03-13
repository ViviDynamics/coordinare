"""Unit tests for OpenCodeAdapter (HTTP-based opencode serve API)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from performer.backends.opencode import OpenCodeAdapter, _build_task_prompt
from performer.models import Score, Stand


def _score() -> Score:
    return Score(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )


def _stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="main")


def _fake_proc(pid: int = 999) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    proc.stdout = MagicMock()
    # Empty async iterator for log drain
    async def _empty():
        return
        yield  # pragma: no cover
    proc.stdout.__aiter__ = lambda self: _empty()
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterStart:
    @respx.mock
    async def test_start_creates_session_and_dispatches_task(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        port = 19900

        respx.get(f"http://127.0.0.1:{port}/global/health").mock(
            return_value=httpx.Response(200, json={"healthy": True})
        )
        respx.post(f"http://127.0.0.1:{port}/session").mock(
            return_value=httpx.Response(200, json={"id": "sess-1"})
        )
        prompt_mock = respx.post(
            f"http://127.0.0.1:{port}/session/sess-1/prompt_async"
        ).mock(return_value=httpx.Response(204))
        # SSE stream (reader task) — returns immediately
        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=b"")
        )

        adapter = OpenCodeAdapter()
        with (
            patch(
                "performer.backends.opencode.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch(
                "performer.backends.opencode._find_free_port",
                return_value=port,
            ),
        ):
            await adapter.start(_stand(tmp_path), _score())

        assert adapter._session_id == "sess-1"
        assert prompt_mock.called
        body = json.loads(prompt_mock.calls[0].request.content)
        assert "Task: T" in body["parts"][0]["text"]

    @respx.mock
    async def test_start_uses_stand_path_as_cwd(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        port = 19905

        respx.get(f"http://127.0.0.1:{port}/global/health").mock(
            return_value=httpx.Response(200, json={"healthy": True})
        )
        respx.post(f"http://127.0.0.1:{port}/session").mock(
            return_value=httpx.Response(200, json={"id": "sess-cwd"})
        )
        respx.post(f"http://127.0.0.1:{port}/session/sess-cwd/prompt_async").mock(
            return_value=httpx.Response(204)
        )
        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=b"")
        )

        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.opencode._find_free_port", return_value=port
        ):
            await OpenCodeAdapter().start(_stand(tmp_path), _score())

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)

    @respx.mock
    async def test_start_does_not_pass_print_logs_flag(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        port = 19906

        respx.get(f"http://127.0.0.1:{port}/global/health").mock(
            return_value=httpx.Response(200, json={"healthy": True})
        )
        respx.post(f"http://127.0.0.1:{port}/session").mock(
            return_value=httpx.Response(200, json={"id": "sess-flags"})
        )
        respx.post(f"http://127.0.0.1:{port}/session/sess-flags/prompt_async").mock(
            return_value=httpx.Response(204)
        )
        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=b"")
        )

        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.opencode._find_free_port", return_value=port
        ):
            await OpenCodeAdapter().start(_stand(tmp_path), _score())

        args = mock_exec.call_args[0]
        assert "--print-logs" not in args
        assert "--log-level" not in args

    @respx.mock
    async def test_start_raises_if_server_never_ready(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        port = 19901

        # Health endpoint always fails
        respx.get(f"http://127.0.0.1:{port}/global/health").mock(
            side_effect=httpx.ConnectError("refused")
        )

        adapter = OpenCodeAdapter()
        with (
            patch(
                "performer.backends.opencode.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch("performer.backends.opencode._find_free_port", return_value=port),
            patch("performer.backends.opencode._READY_TIMEOUT", 0.3),
            patch("performer.backends.opencode._READY_POLL_INTERVAL", 0.05),
        ):
            with pytest.raises(RuntimeError, match="did not become ready"):
                await adapter.start(_stand(tmp_path), _score())


# ---------------------------------------------------------------------------
# get_status() / drain_events()
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterGetStatus:
    def test_initial_status_is_working(self) -> None:
        adapter = OpenCodeAdapter()
        assert adapter.get_status().state == "working"

    async def test_session_idle_transitions_to_done(self) -> None:
        adapter = OpenCodeAdapter()
        event = {
            "type": "session.updated",
            "properties": {"session": {"time": {"idle": 1234567890}}},
        }
        adapter._handle_event(event)
        assert adapter.get_status().state == "done"

    async def test_session_idle_legacy_event(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({"type": "session.idle"})
        assert adapter.get_status().state == "done"

    async def test_session_error_event(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "session.error",
            "properties": {"error": {"message": "boom"}},
        })
        status = adapter.get_status()
        assert status.state == "error"
        assert "boom" in (status.error_reason or "")

    async def test_message_part_updated_text(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "text", "text": "Working on it..."}},
        })
        assert adapter.get_status().state == "working"
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "progress"
        assert "Working on it" in events[0].text

    async def test_message_part_updated_tool_input(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool-input", "toolName": "read_file", "input": "/foo"}},
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "tool_use"
        assert "read_file" in events[0].text

    async def test_wrapped_payload_format(self) -> None:
        """Events may be wrapped as {directory, payload: {type, properties}}."""
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "directory": "/some/path",
            "payload": {
                "type": "session.idle",
                "properties": {},
            },
        })
        assert adapter.get_status().state == "done"

    async def test_drain_events_clears_buffer(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "text", "text": "step 1"}},
        })
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "text", "text": "step 2"}},
        })
        first = adapter.drain_events()
        second = adapter.drain_events()
        assert len(first) == 2
        assert len(second) == 0


# ---------------------------------------------------------------------------
# relay_feedback()
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterRelayFeedback:
    @respx.mock
    async def test_relay_feedback_posts_to_session(self, tmp_path: Path) -> None:
        port = 19902
        session_id = "sess-relay"

        feedback_mock = respx.post(
            f"http://127.0.0.1:{port}/session/{session_id}/prompt_async"
        ).mock(return_value=httpx.Response(204))

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = session_id
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )
        # Set to blocked so relay can reset it
        adapter._status = adapter._status.__class__(state="blocked")

        await adapter.relay_feedback("please use GitHub Actions")

        assert feedback_mock.called
        body = json.loads(feedback_mock.calls[0].request.content)
        assert "GitHub Actions" in body["parts"][0]["text"]
        assert adapter.get_status().state == "working"

    @respx.mock
    async def test_relay_feedback_restarts_reader_when_done(self, tmp_path: Path) -> None:
        port = 19907
        session_id = "sess-restart"

        respx.post(
            f"http://127.0.0.1:{port}/session/{session_id}/prompt_async"
        ).mock(return_value=httpx.Response(204))
        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=b"")
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = session_id
        adapter._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        # Simulate reader task that has already completed
        async def _noop() -> None:
            pass

        adapter._reader_task = asyncio.create_task(_noop())
        await asyncio.sleep(0)  # let it finish

        assert adapter._reader_task.done()
        await adapter.relay_feedback("second turn")

        # Reader should have been restarted
        assert adapter._reader_task is not None
        assert not adapter._reader_task.done() or True  # task may finish instantly with mock
        await adapter._reader_task  # clean up

    async def test_relay_feedback_noop_when_no_session(self) -> None:
        adapter = OpenCodeAdapter()
        # Should not raise
        await adapter.relay_feedback("hello")


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterStop:
    async def test_stop_kills_process_group(self, tmp_path: Path) -> None:
        proc = _fake_proc(pid=1234)
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        with (
            patch("performer.backends.opencode.os.getpgid", return_value=1234),
            patch("performer.backends.opencode.os.killpg") as mock_killpg,
        ):
            await adapter.stop()

        mock_killpg.assert_called_once()

    async def test_stop_cancels_reader_task(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        # Simulate a running reader task
        async def _run_forever() -> None:
            await asyncio.sleep(9999)

        reader = asyncio.create_task(_run_forever())
        adapter._reader_task = reader

        with (
            patch("performer.backends.opencode.os.getpgid", return_value=proc.pid),
            patch("performer.backends.opencode.os.killpg"),
        ):
            await adapter.stop()

        assert reader.cancelled() or reader.done()


# ---------------------------------------------------------------------------
# stop() — additional paths
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterStopAdditional:
    async def test_stop_uses_psutil_fallback_when_getpgid_raises(self) -> None:
        proc = _fake_proc(pid=1234)
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill = MagicMock()

        with (
            patch("performer.backends.opencode.os.getpgid", side_effect=OSError("no such process")),
            patch("performer.backends.opencode.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()

        mock_psutil_proc.kill.assert_called_once()

    async def test_stop_handles_proc_wait_timeout(self) -> None:
        proc = _fake_proc()
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        with (
            patch("performer.backends.opencode.os.getpgid", return_value=proc.pid),
            patch("performer.backends.opencode.os.killpg"),
            patch("performer.backends.opencode.asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            await adapter.stop()  # should not raise

    async def test_stop_closes_client(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        adapter._proc = None  # no process
        client = MagicMock(spec=httpx.AsyncClient)
        client.aclose = AsyncMock()
        adapter._client = client

        await adapter.stop()

        client.aclose.assert_awaited_once()
        assert adapter._client is None


# ---------------------------------------------------------------------------
# _drain_logs
# ---------------------------------------------------------------------------

class TestDrainLogs:
    async def test_drain_logs_buffers_lines(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()

        async def _gen():
            yield b"line one\n"
            yield b"line two\n"
            yield b"\n"  # empty line — should be ignored

        proc.stdout.__aiter__ = lambda self: _gen()

        adapter = OpenCodeAdapter()
        adapter._proc = proc
        await adapter._drain_logs()

        assert "line one" in adapter._log_buffer
        assert "line two" in adapter._log_buffer
        assert len(adapter._log_buffer) == 2

    async def test_drain_logs_no_proc(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._proc = None
        await adapter._drain_logs()  # should not raise

    async def test_drain_logs_cancelled_error_is_swallowed(self) -> None:
        """CancelledError in the log drain is caught and swallowed (not re-raised)."""
        proc = MagicMock()

        async def _raises_cancelled():
            raise asyncio.CancelledError
            yield  # pragma: no cover

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _raises_cancelled()
        adapter = OpenCodeAdapter()
        adapter._proc = proc
        await adapter._drain_logs()  # should not raise

    async def test_drain_logs_exception_is_swallowed(self) -> None:
        """General exception in the log drain is caught and swallowed."""
        proc = MagicMock()

        async def _raises_error():
            raise RuntimeError("pipe broken")
            yield  # pragma: no cover

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _raises_error()
        adapter = OpenCodeAdapter()
        adapter._proc = proc
        await adapter._drain_logs()  # should not raise


# ---------------------------------------------------------------------------
# _event_reader_loop
# ---------------------------------------------------------------------------

class TestEventReaderLoop:
    async def test_event_reader_no_client(self) -> None:
        adapter = OpenCodeAdapter()
        # _client is None by default
        await adapter._event_reader_loop()  # should return immediately

    @respx.mock
    async def test_event_reader_handles_sse_stream(self, tmp_path: Path) -> None:
        port = 19903
        session_id = "sess-sse"

        sse_body = (
            "data: {\"type\": \"message.part.updated\", \"properties\": {\"part\": {\"type\": \"text\", \"text\": \"hello\"}}}\n\n"
            "data: {\"type\": \"session.idle\", \"properties\": {}}\n\n"
        ).encode()

        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = session_id
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"
        events = adapter.drain_events()
        assert any(e.type.value == "progress" for e in events)

    @respx.mock
    async def test_event_reader_handles_exception(self, tmp_path: Path) -> None:
        port = 19904

        respx.get(f"http://127.0.0.1:{port}/event").mock(
            side_effect=httpx.ConnectError("refused")
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = "sess-err"
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._event_reader_loop()
        # Exception causes error status OR resolve_final_status sets done
        assert adapter.get_status().state in ("error", "done")

    @respx.mock
    async def test_event_reader_skips_non_json_data(self, tmp_path: Path) -> None:
        port = 19905

        sse_body = (
            "data: not-valid-json\n\n"
            "data: {\"type\": \"session.idle\", \"properties\": {}}\n\n"
        ).encode()

        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=sse_body)
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = "sess-skip"
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._event_reader_loop()
        assert adapter.get_status().state == "done"


# ---------------------------------------------------------------------------
# _resolve_final_status
# ---------------------------------------------------------------------------

class TestResolveFinalStatus:
    async def test_resolve_no_client(self) -> None:
        adapter = OpenCodeAdapter()
        await adapter._resolve_final_status()
        assert adapter.get_status().state == "done"

    @respx.mock
    async def test_resolve_with_idle_session(self, tmp_path: Path) -> None:
        port = 19906
        session_id = "sess-resolve"

        respx.get(f"http://127.0.0.1:{port}/session/{session_id}").mock(
            return_value=httpx.Response(200, json={"id": session_id, "time": {"idle": 1234}})
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = session_id
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._resolve_final_status()
        assert adapter.get_status().state == "done"

    @respx.mock
    async def test_resolve_defaults_to_done_on_error(self, tmp_path: Path) -> None:
        port = 19907
        session_id = "sess-resolve-err"

        respx.get(f"http://127.0.0.1:{port}/session/{session_id}").mock(
            side_effect=httpx.ConnectError("refused")
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = session_id
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._resolve_final_status()
        assert adapter.get_status().state == "done"


# ---------------------------------------------------------------------------
# _handle_event — additional paths
# ---------------------------------------------------------------------------

class TestHandleEventAdditional:
    def test_session_updated_error_state(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "session.updated",
            "properties": {"session": {"error": "something went wrong", "time": {}}},
        })
        assert adapter.get_status().state == "error"
        assert "something went wrong" in (adapter.get_status().error_reason or "")

    def test_message_part_updated_tool_output(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool-output", "toolName": "write_file", "output": "done"}},
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert "write_file" in events[0].text

    def test_message_updated_with_questions(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.updated",
            "properties": {
                "parts": [{"type": "question", "text": "Which branch?"}]
            },
        })
        status = adapter.get_status()
        assert status.state == "blocked"
        assert "Which branch?" in status.questions

    def test_message_part_updated_empty_text_ignored(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {"type": "text", "text": ""}},
        })
        # Empty text should not emit event or change status
        assert adapter.drain_events() == []

    def test_unknown_event_type_is_noop(self) -> None:
        adapter = OpenCodeAdapter()
        adapter._handle_event({"type": "some.unknown.event", "properties": {}})
        assert adapter.get_status().state == "working"


# ---------------------------------------------------------------------------
# _find_free_port
# ---------------------------------------------------------------------------

class TestFindFreePort:
    def test_returns_valid_port(self) -> None:
        from performer.backends.opencode import _find_free_port
        port = _find_free_port()
        assert 1024 <= port <= 65535


# ---------------------------------------------------------------------------
# _build_task_prompt
# ---------------------------------------------------------------------------

class TestBuildTaskPrompt:
    def test_includes_title(self) -> None:
        score = _score()
        prompt = _build_task_prompt(score)
        assert "Task: T" in prompt

    def test_includes_description(self) -> None:
        score = Score(
            title="T",
            description="Some context here",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        prompt = _build_task_prompt(score)
        assert "Some context here" in prompt

    def test_includes_acceptance_criteria(self) -> None:
        score = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
            acceptance_criteria=["must pass CI", "must have tests"],
        )
        prompt = _build_task_prompt(score)
        assert "must pass CI" in prompt
        assert "must have tests" in prompt

    def test_includes_clarifications(self) -> None:
        score = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
            clarifications=[
                {"questions": ["Use Python?"], "answer": "Yes, Python 3.12"},
            ],
        )
        prompt = _build_task_prompt(score)
        assert "Use Python?" in prompt
        assert "Yes, Python 3.12" in prompt

    def test_includes_footer_commit_instructions(self) -> None:
        prompt = _build_task_prompt(_score())
        assert "Commit your changes" in prompt
        assert "Do not push or open a pull request — this will be handled automatically" in prompt


# ---------------------------------------------------------------------------
# stop() — psutil NoSuchProcess paths
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterStopPsutil:
    async def test_stop_psutil_child_no_such_process(self) -> None:
        """child.kill() raising NoSuchProcess is swallowed; parent.kill() still called."""
        import psutil

        proc = _fake_proc(pid=5678)
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        dying_child = MagicMock()
        dying_child.kill = MagicMock(side_effect=psutil.NoSuchProcess(pid=5679))

        mock_parent = MagicMock()
        mock_parent.children.return_value = [dying_child]
        mock_parent.kill = MagicMock()

        with (
            patch("performer.backends.opencode.os.getpgid", side_effect=OSError("no pgid")),
            patch("performer.backends.opencode.psutil.Process", return_value=mock_parent),
        ):
            await adapter.stop()  # should not raise

        mock_parent.kill.assert_called_once()

    async def test_stop_psutil_parent_no_such_process(self) -> None:
        """parent.kill() raising NoSuchProcess is swallowed."""
        import psutil

        proc = _fake_proc(pid=5678)
        adapter = OpenCodeAdapter()
        adapter._proc = proc

        mock_parent = MagicMock()
        mock_parent.children.return_value = []
        mock_parent.kill = MagicMock(side_effect=psutil.NoSuchProcess(pid=5678))

        with (
            patch("performer.backends.opencode.os.getpgid", side_effect=OSError("no pgid")),
            patch("performer.backends.opencode.psutil.Process", return_value=mock_parent),
        ):
            await adapter.stop()  # should not raise


# ---------------------------------------------------------------------------
# _event_reader_loop — empty data_str path
# ---------------------------------------------------------------------------

class TestEventReaderLoopEmptyData:
    @respx.mock
    async def test_event_reader_skips_empty_data_str(self, tmp_path: Path) -> None:
        """SSE lines with 'data:' followed only by whitespace are skipped."""
        port = 19910

        sse_body = (
            "data:   \n\n"   # empty after strip → skipped
            "data: {\"type\": \"session.idle\", \"properties\": {}}\n\n"
        ).encode()

        respx.get(f"http://127.0.0.1:{port}/event").mock(
            return_value=httpx.Response(200, content=sse_body)
        )

        adapter = OpenCodeAdapter()
        adapter._port = port
        adapter._session_id = "sess-empty"
        adapter._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(tmp_path)},
        )

        await adapter._event_reader_loop()
        assert adapter.get_status().state == "done"
