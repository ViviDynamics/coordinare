"""Unit tests for ClaudeCodeBackend (--resume / session_id implementation)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest


from performer.backends.claude_code import ClaudeCodeBackend, _build_task_prompt
from performer.models import Score, Stand


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Test Task",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    defaults.update(kwargs)
    return Score(**defaults)


def _stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="main")


def _fake_proc(pid: int = 42) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    async def _empty():
        return
        yield  # pragma: no cover

    proc.stdout = MagicMock()
    proc.stdout.__aiter__ = lambda self: _empty()
    proc.stderr = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestClaudeCodeBackendStart:
    async def test_start_launches_with_stream_json(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        mock_exec.assert_awaited_once()
        args = mock_exec.call_args[0]
        assert "claude" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--include-partial-messages" in args
        assert "--print" in args

    async def test_start_does_not_use_resume_on_first_call(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = mock_exec.call_args[0]
        assert "--resume" not in args

    async def test_start_passes_prompt_via_p_flag(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        assert "Test Task" in args[p_idx + 1]

    async def test_start_merges_cache_env_into_subprocess_env(self, tmp_path: Path) -> None:
        """060: cache_env from activate.sh must be visible to the agent subprocess."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GIT_AUTHOR_NAME": "performer"},
            cache_env={"PATH": "/devenv/foo/bin:/usr/bin", "VIRTUAL_ENV": "/devenv/foo/.venv"},
        )

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        assert env["VIRTUAL_ENV"] == "/devenv/foo/.venv"
        assert env["PATH"] == "/devenv/foo/bin:/usr/bin"
        assert env["GIT_AUTHOR_NAME"] == "performer"

    async def test_start_git_env_overrides_cache_env_on_conflict(self, tmp_path: Path) -> None:
        """060: precedence is os.environ < cache_env < git_env — git auth must win."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GITHUB_TOKEN": "real-token"},
            cache_env={"GITHUB_TOKEN": "stale-cached"},
        )

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        assert env["GITHUB_TOKEN"] == "real-token"

    async def test_start_injects_tool_env_for_cli_shims(self, tmp_path: Path) -> None:
        """Bug 16.2: score.tool_env must be merged into the subprocess env so
        in-container CLI shims (e.g. performer-upload-screenshot) can read
        PERFORMER_GH_* context."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        stand = Stand(path=tmp_path, branch="main")
        score = _score(
            github_token="ghp_xyz",
            pr_url="https://github.com/org/repo/pull/77",
            issue_number=42,
        )

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, score)

        env = mock_exec.call_args[1]["env"]
        assert env["PERFORMER_GH_TOKEN"] == "ghp_xyz"
        assert env["PERFORMER_GH_OWNER"] == "org"
        assert env["PERFORMER_GH_REPO"] == "repo"
        # PR number takes precedence over issue_number
        assert env["PERFORMER_GH_ISSUE"] == "77"


# ---------------------------------------------------------------------------
# get_status() / drain_events()
# ---------------------------------------------------------------------------

class TestClaudeCodeBackendGetStatus:
    def test_initial_status_is_working(self) -> None:
        adapter = ClaudeCodeBackend()
        assert adapter.get_status().state == "working"

    def test_drain_events_returns_and_clears(self) -> None:
        adapter = ClaudeCodeBackend()
        from performer.models import BackendEvent, BackendEventType
        from datetime import UTC, datetime
        adapter._event_buffer.append(
            BackendEvent(type=BackendEventType.progress, text="hello", timestamp=datetime.now(UTC))
        )
        first = adapter.drain_events()
        second = adapter.drain_events()
        assert len(first) == 1
        assert len(second) == 0

    def test_handle_event_assistant_text(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Working on it"}]
            },
        })
        assert adapter.get_status().progress == "Working on it"
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "progress"

    def test_handle_event_assistant_tool_use(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "read_file"}]
            },
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "tool_use"
        assert "read_file" in events[0].text

    def test_handle_event_assistant_thinking(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "reasoning..."}]
            },
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "thinking"

    def test_handle_event_tool_result_string(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "tool_result",
            "tool_use_id": "tool123",
            "content": "file contents here",
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert "result:" in events[0].text

    def test_handle_event_tool_result_list(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "tool_result",
            "tool_use_id": "tool456",
            "content": [{"text": "part one"}, {"text": "part two"}],
        })
        events = adapter.drain_events()
        assert len(events) == 1
        assert "part one" in events[0].text

    def test_handle_event_result_success(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.0025,
        })
        status = adapter.get_status()
        assert status.state == "done"
        assert status.tokens_processed == 150
        events = adapter.drain_events()
        assert len(events) == 1
        assert "150" in events[0].text
        assert "$0.0025" in events[0].text

    def test_handle_event_result_success_no_cost(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "input_tokens": 10,
            "output_tokens": 5,
        })
        assert adapter.get_status().state == "done"
        events = adapter.drain_events()
        assert "$" not in events[0].text

    def test_handle_event_result_success_captures_session_id(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "session_id": "sess-result-123",
            "input_tokens": 10,
            "output_tokens": 5,
        })
        assert adapter._session_id == "sess-result-123"

    def test_handle_event_result_error(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "error",
            "error": "rate limit exceeded",
        })
        status = adapter.get_status()
        assert status.state == "error"
        assert "rate limit" in (status.error_reason or "")

    def test_handle_event_result_interrupted(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "result", "subtype": "interrupted"})
        assert adapter.get_status().state == "error"

    def test_handle_event_system_init_captures_session_id(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "system",
            "subtype": "init",
            "session_id": "sess-abc-123",
        })
        assert adapter._session_id == "sess-abc-123"
        assert adapter.get_status().state == "working"

    def test_handle_event_system_init_missing_session_id_noop(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "system", "subtype": "init"})
        assert adapter._session_id is None

    def test_handle_event_system_other_subtype_is_noop(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "system", "subtype": "other"})
        assert adapter.get_status().state == "working"
        assert adapter.drain_events() == []

    def test_handle_event_unknown_is_noop(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "unknown_event"})
        assert adapter.get_status().state == "working"

    def test_handle_event_result_success_usage_fallback(self) -> None:
        """Tokens from usage dict when top-level tokens missing."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "usage": {"input_tokens": 20, "output_tokens": 30},
        })
        assert adapter.get_status().tokens_processed == 50


# ---------------------------------------------------------------------------
# _event_reader_loop
# ---------------------------------------------------------------------------

class TestEventReaderLoop:
    async def test_reader_handles_valid_events(self) -> None:
        events_json = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "step"}]}}) + "\n",
            json.dumps({"type": "result", "subtype": "success", "input_tokens": 1, "output_tokens": 1}) + "\n",
        ]

        async def _gen():
            for line in events_json:
                yield line.encode()

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()
        proc.returncode = 0

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_skips_non_json_lines(self) -> None:
        async def _gen():
            yield b"not valid json\n"
            yield json.dumps({"type": "result", "subtype": "success", "input_tokens": 0, "output_tokens": 0}).encode() + b"\n"

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()
        proc.returncode = 0

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_skips_empty_lines(self) -> None:
        async def _gen():
            yield b"\n"
            yield b"   \n"
            yield json.dumps({"type": "result", "subtype": "success", "input_tokens": 0, "output_tokens": 0}).encode() + b"\n"

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()
        proc.returncode = 0

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_sets_error_on_exception(self) -> None:
        async def _gen():
            raise RuntimeError("pipe error")
            yield  # pragma: no cover

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"

    async def test_reader_finalizes_done_on_zero_exit(self) -> None:
        """When process exits cleanly without result event, status → done."""
        async def _gen():
            return
            yield  # pragma: no cover

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()
        proc.returncode = None
        proc.wait = AsyncMock(side_effect=lambda: setattr(proc, "returncode", 0) or 0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_finalizes_error_on_nonzero_exit(self) -> None:
        async def _gen():
            return
            yield  # pragma: no cover

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _gen()
        proc.returncode = None
        proc.wait = AsyncMock(side_effect=lambda: setattr(proc, "returncode", 1) or 1)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"

    async def test_reader_no_proc_returns_immediately(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._proc = None
        await adapter._event_reader_loop()  # should not raise


# ---------------------------------------------------------------------------
# relay_feedback()
# ---------------------------------------------------------------------------

class TestRelayFeedback:
    async def test_relay_feedback_uses_resume_when_session_id_known(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        adapter._stand = _stand(tmp_path)
        adapter._session_id = "sess-xyz-789"

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.claude_code.os.getpgid", return_value=proc.pid
        ), patch(
            "performer.backends.claude_code.os.killpg"
        ):
            await adapter.relay_feedback("use black for formatting")

        args = mock_exec.call_args[0]
        assert "--resume" in args
        idx = list(args).index("--resume")
        assert args[idx + 1] == "sess-xyz-789"
        assert "-p" in args
        p_idx = list(args).index("-p")
        assert args[p_idx + 1] == "use black for formatting"
        assert adapter.get_status().state == "working"

    async def test_relay_feedback_no_resume_when_no_session_id(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        adapter._stand = _stand(tmp_path)
        adapter._session_id = None

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.claude_code.os.getpgid", return_value=proc.pid
        ), patch(
            "performer.backends.claude_code.os.killpg"
        ):
            await adapter.relay_feedback("feedback without session")

        args = mock_exec.call_args[0]
        assert "--resume" not in args

    async def test_relay_feedback_noop_when_no_stand(self) -> None:
        adapter = ClaudeCodeBackend()
        await adapter.relay_feedback("feedback")  # should not raise, no subprocess

    async def test_relay_feedback_relaunches_subprocess(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
            first_count = mock_exec.await_count

            with (
                patch("performer.backends.claude_code.os.getpgid", return_value=proc.pid),
                patch("performer.backends.claude_code.os.killpg"),
            ):
                await adapter.relay_feedback("try again")

        assert mock_exec.await_count == first_count + 1


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    async def test_stop_kills_process_group(self, tmp_path: Path) -> None:
        proc = _fake_proc(pid=5678)
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        with (
            patch("performer.backends.claude_code.os.getpgid", return_value=5678),
            patch("performer.backends.claude_code.os.killpg") as mock_killpg,
        ):
            await adapter.stop()

        mock_killpg.assert_called_once()

    async def test_stop_uses_psutil_fallback(self, tmp_path: Path) -> None:
        proc = _fake_proc(pid=7777)
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill = MagicMock()

        with (
            patch("performer.backends.claude_code.os.getpgid", side_effect=OSError("nope")),
            patch("performer.backends.claude_code.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()

        mock_psutil_proc.kill.assert_called_once()

    async def test_stop_psutil_child_no_such_process(self, tmp_path: Path) -> None:
        """Child that raises NoSuchProcess during psutil fallback is skipped."""
        proc = _fake_proc(pid=8888)
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        dying_child = MagicMock()
        dying_child.kill = MagicMock(side_effect=psutil.NoSuchProcess(pid=8888))
        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = [dying_child]
        mock_psutil_proc.kill = MagicMock()

        with (
            patch("performer.backends.claude_code.os.getpgid", side_effect=OSError("no pgid")),
            patch("performer.backends.claude_code.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()  # should not raise

        dying_child.kill.assert_called_once()
        mock_psutil_proc.kill.assert_called_once()

    async def test_stop_psutil_parent_no_such_process(self, tmp_path: Path) -> None:
        """Parent.kill() raising NoSuchProcess is handled gracefully."""
        proc = _fake_proc(pid=9999)
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill = MagicMock(side_effect=psutil.NoSuchProcess(pid=9999))

        with (
            patch("performer.backends.claude_code.os.getpgid", side_effect=OSError("nope")),
            patch("performer.backends.claude_code.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()  # should not raise

    async def test_stop_wait_timeout(self, tmp_path: Path) -> None:
        """asyncio.TimeoutError while waiting for proc.wait() is swallowed."""
        proc = _fake_proc(pid=1234)
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        async def _timeout_wait_for(coro, **kwargs: object) -> None:
            coro.close()
            raise asyncio.TimeoutError()

        with (
            patch("performer.backends.claude_code.os.getpgid", return_value=1234),
            patch("performer.backends.claude_code.os.killpg"),
            patch("performer.backends.claude_code.asyncio.wait_for", new=_timeout_wait_for),
        ):
            await adapter.stop()  # should not raise

    async def test_stop_noop_when_no_proc(self) -> None:
        adapter = ClaudeCodeBackend()
        await adapter.stop()  # should not raise

    async def test_stop_noop_when_proc_already_exited(self, tmp_path: Path) -> None:
        proc = _fake_proc(pid=1111)
        proc.returncode = 0  # already exited
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        await adapter.stop()  # should not attempt kill


# ---------------------------------------------------------------------------
# _event_reader_loop edge cases
# ---------------------------------------------------------------------------

class TestEventReaderLoopCancellation:
    async def test_cancelled_error_is_reraised(self) -> None:
        """CancelledError propagates out of the reader loop."""
        adapter = ClaudeCodeBackend()
        proc = MagicMock()

        async def _raises_cancelled():
            raise asyncio.CancelledError
            yield  # pragma: no cover

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _raises_cancelled()
        proc.returncode = 0
        adapter._proc = proc

        with pytest.raises(asyncio.CancelledError):
            await adapter._event_reader_loop()

    async def test_finally_waits_for_proc_when_returncode_none(self) -> None:
        """Finally block calls proc.wait() when proc is still running at loop exit."""
        adapter = ClaudeCodeBackend()
        proc = MagicMock()
        proc.returncode = None  # still running

        async def _empty():
            return
            yield  # pragma: no cover

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _empty()

        async def _set_returncode():
            proc.returncode = 0

        proc.wait = AsyncMock(side_effect=_set_returncode)
        adapter._proc = proc

        await adapter._event_reader_loop()

        proc.wait.assert_awaited_once()
        assert adapter.get_status().state == "done"

    async def test_finally_nonzero_exit_sets_error(self) -> None:
        """Finally block: proc exits non-zero → error state."""
        adapter = ClaudeCodeBackend()
        proc = MagicMock()
        proc.returncode = None

        async def _empty():
            return
            yield  # pragma: no cover

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _empty()

        async def _set_returncode():
            proc.returncode = 1

        proc.wait = AsyncMock(side_effect=_set_returncode)
        adapter._proc = proc

        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"
        assert "1" in adapter.get_status().error_reason


class TestHandleEventEdgeCases:
    def test_unknown_block_type_in_assistant_is_noop(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "assistant",
            "message": {"content": [{"type": "unknown_block_type", "data": "..."}]},
        })
        assert adapter.get_status().state == "working"
        assert adapter.drain_events() == []

    def test_result_unknown_subtype_is_noop(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "result", "subtype": "unknown_subtype"})
        assert adapter.get_status().state == "working"


# ---------------------------------------------------------------------------
# _build_task_prompt
# ---------------------------------------------------------------------------

class TestBuildTaskPrompt:
    def test_includes_title(self) -> None:
        prompt = _build_task_prompt(_score())
        assert "Test Task" in prompt

    def test_includes_description(self) -> None:
        score = _score(description="Do the thing carefully")
        prompt = _build_task_prompt(score)
        assert "Do the thing carefully" in prompt

    def test_includes_acceptance_criteria(self) -> None:
        score = _score(acceptance_criteria=["must pass CI"])
        prompt = _build_task_prompt(score)
        assert "must pass CI" in prompt

    def test_includes_clarifications(self) -> None:
        score = _score(clarifications=[
            {"questions": ["Which language?"], "answer": "Python"}
        ])
        prompt = _build_task_prompt(score)
        assert "Which language?" in prompt
        assert "Python" in prompt

    def test_includes_footer_instructions(self) -> None:
        prompt = _build_task_prompt(_score())
        assert "Commit your changes" in prompt
        assert "Do not push or open a pull request — this will be handled automatically" in prompt

    def test_reviewer_role_uses_json_only_footer(self) -> None:
        prompt = _build_task_prompt(_score(role="reviewing"))
        assert "Return ONLY a valid JSON object" in prompt
        assert "Do not include markdown, prose, or code fences." in prompt
        assert "Commit your changes" not in prompt

    def test_reviewer_noun_role_uses_json_only_footer(self) -> None:
        prompt = _build_task_prompt(_score(role="reviewer"))
        assert "Return ONLY a valid JSON object" in prompt
        assert "Commit your changes" not in prompt

    def test_qa_role_includes_verification_contract_hints(self) -> None:
        prompt = _build_task_prompt(_score(role="qa"))
        assert "verification_steps" in prompt
        assert "visual_evidence" in prompt
        assert "visual_validation_required" in prompt
        assert "demo_setup_steps" in prompt
        assert "visual_capture_commands" in prompt
        assert "visual_capture_blockers" in prompt

    def test_clarifications_without_answer(self) -> None:
        score = _score(clarifications=[{"questions": ["Q?"], "answer": ""}])
        prompt = _build_task_prompt(score)
        assert "Q?" in prompt

    def test_clarifications_without_questions(self) -> None:
        score = _score(clarifications=[{"questions": [], "answer": "Some answer"}])
        prompt = _build_task_prompt(score)
        assert "Some answer" in prompt
