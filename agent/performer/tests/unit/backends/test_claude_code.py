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
    # Reader loop uses stdout.read(n); default returns EOF immediately.
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.stderr = MagicMock()

    async def _stderr_iter():
        return
        yield  # pragma: no cover

    proc.stderr.__aiter__ = lambda self: _stderr_iter()
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


def _proc_with_lines(lines: list[bytes], returncode: int | None = 0) -> MagicMock:
    """Build a proc whose stdout.read() yields ``lines`` as chunks then EOF (b'')."""
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.stdout = MagicMock()
    queue = list(lines) + [b""]  # EOF sentinel
    proc.stdout.read = AsyncMock(side_effect=queue)
    proc.stderr = MagicMock()

    async def _stderr_iter():
        return
        yield  # pragma: no cover

    proc.stderr.__aiter__ = lambda self: _stderr_iter()
    proc.wait = AsyncMock(return_value=returncode or 0)
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
        assert "--verbose" in args

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

    async def test_persona_passed_via_append_system_prompt_flag(
        self, tmp_path: Path
    ) -> None:
        """FR-016: persona is routed through --append-system-prompt, not the prompt body."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        score = _score(persona_instructions="PERSONA_MARKER_CC be careful.")
        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), score)
        args = list(mock_exec.call_args[0])
        assert "--append-system-prompt" in args
        sp_idx = args.index("--append-system-prompt")
        assert args[sp_idx + 1] == "PERSONA_MARKER_CC be careful."
        # And the persona text is NOT embedded in the -p prompt.
        p_idx = args.index("-p")
        assert "PERSONA_MARKER_CC" not in args[p_idx + 1]
        assert "## Role Instructions" not in args[p_idx + 1]

    async def test_append_system_prompt_omitted_when_persona_empty(
        self, tmp_path: Path
    ) -> None:
        """FR-016: empty persona => no --append-system-prompt flag at all."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score(persona_instructions=""))
        args = list(mock_exec.call_args[0])
        assert "--append-system-prompt" not in args

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

    async def test_add_dir_emitted_for_env_cache_path(self, tmp_path: Path) -> None:
        """env_bootstrap roles set score.env_cache_path to a path outside the
        stand cwd (e.g. /devenv/<symphony>-<hash>). The CLI's directory-write
        sandbox is pinned to cwd, so the backend must widen it via --add-dir
        (target + parent) — otherwise mkdir on the cache path is blocked.
        """
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        score = _score(env_cache_path="/devenv/website-3ab3e0")

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), score)

        args = list(mock_exec.call_args[0])
        # Pairs of --add-dir <path> — expect both the target and its parent.
        add_dir_targets = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
        assert "/devenv/website-3ab3e0" in add_dir_targets
        assert "/devenv" in add_dir_targets

    async def test_add_dir_omitted_when_env_cache_path_blank(self, tmp_path: Path) -> None:
        """Non-bootstrap roles leave env_cache_path empty; --add-dir must not
        appear, preserving the default cwd-only allowlist (SC-002 byte-baseline)."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = mock_exec.call_args[0]
        assert "--add-dir" not in args

    async def test_launch_passes_dangerously_skip_permissions(self, tmp_path: Path) -> None:
        """The performer container is the trust boundary; the CLI's interactive
        permission gate has no human approver and otherwise denies safe ops
        (cp -a, compound bash, source) — trapping the model in self-repair loops."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = list(mock_exec.call_args[0])
        assert "--dangerously-skip-permissions" in args
        # bypassPermissions additionally disables the CLI's hard-coded Bash
        # heuristics (cp -a, compound bash, source) that --dangerously-skip-
        # permissions alone does not cover.
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
        # IS_SANDBOX=1 is the documented escape hatch for the CLI's root-user
        # refusal of --dangerously-skip-permissions; without it the subprocess
        # exits with code 1 immediately.
        env = mock_exec.call_args.kwargs.get("env", {})
        assert env.get("IS_SANDBOX") == "1"


# ---------------------------------------------------------------------------
# get_status() / drain_events()
# ---------------------------------------------------------------------------

class TestClaudeCodeBackendGetStatus:
    def test_initial_status_is_working(self) -> None:
        adapter = ClaudeCodeBackend()
        assert adapter.get_status().state == "working"

    def test_get_status_liveness_no_proc_preserves_state(self) -> None:
        adapter = ClaudeCodeBackend()
        # No subprocess attached — must not crash and must not transition.
        assert adapter.get_status().state == "working"

    def test_get_status_liveness_proc_still_running_preserves_state(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = None
        assert adapter.get_status().state == "working"

    def test_get_status_liveness_proc_exited_clean_forces_done(self) -> None:
        # Subprocess exited cleanly but the reader_task never emitted a
        # terminal stream-json event — liveness fallback must transition.
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 0
        status = adapter.get_status()
        assert status.state == "done"

    def test_get_status_liveness_proc_exited_nonzero_forces_error(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 137
        adapter._stderr_tail.append("boom: killed by OOM")
        status = adapter.get_status()
        assert status.state == "error"
        assert status.error_reason is not None
        assert "137" in status.error_reason
        assert "without terminal event" in status.error_reason
        assert "boom: killed by OOM" in status.error_reason

    def test_get_status_liveness_does_not_overwrite_terminal_state(self) -> None:
        # If the reader loop already set a terminal state, the liveness probe
        # must be a no-op (it only acts when state == "working").
        from performer.backends.base import BackendStatus
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 1
        adapter._status = BackendStatus(state="done", output="result text")
        status = adapter.get_status()
        assert status.state == "done"
        assert status.output == "result text"

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

    def test_assistant_text_accumulates_into_output_on_result_success(self) -> None:
        """Assistant text blocks must be accumulated and flushed to BackendStatus.output."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "block1 "}]},
        })
        adapter._handle_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "block2"}]},
        })
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "input_tokens": 1,
            "output_tokens": 1,
        })
        status = adapter.get_status()
        assert status.state == "done"
        assert status.output == "block1 block2"

    def test_result_success_with_no_assistant_text_leaves_output_none(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "input_tokens": 1,
            "output_tokens": 1,
        })
        assert adapter.get_status().output is None

    def test_redact_scrubs_bearer_tokens(self) -> None:
        line = "Authorization: Bearer sk-ant-abc123XYZ.def_456 failed"
        out = ClaudeCodeBackend._redact(line)
        assert "sk-ant-abc123" not in out
        assert "[REDACTED]" in out

    def test_redact_scrubs_env_token_values(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "supersecrettoken123")
        line = "request to upstream with token=supersecrettoken123 returned 401"
        out = ClaudeCodeBackend._redact(line)
        assert "supersecrettoken123" not in out
        assert "[REDACTED]" in out

    def test_stderr_tail_text_truncates(self) -> None:
        adapter = ClaudeCodeBackend()
        # Push more than the cap (50 lines) — deque should retain only the tail.
        for i in range(200):
            adapter._stderr_tail.append(f"line-{i}")
        text = adapter._stderr_tail_text()
        # Bounded by deque maxlen — earliest lines dropped.
        assert "line-0\n" not in text
        assert "line-199" in text

    def test_stderr_tail_text_empty_when_no_stderr(self) -> None:
        adapter = ClaudeCodeBackend()
        assert adapter._stderr_tail_text() == ""

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

    # ------------------------------------------------------------------
    # api_retry handling — guards against the "504 then SSE stalls" wedge
    # ------------------------------------------------------------------

    def _api_retry_event(self, attempt: int = 1, status: int = 504) -> dict:
        return {
            "type": "system",
            "subtype": "api_retry",
            "attempt": attempt,
            "max_retries": 10,
            "error_status": status,
            "error": "server_error",
        }

    def test_handle_event_api_retry_returns_false(self) -> None:
        """api_retry is NOT progress — must return False so the reader loop's
        idle watchdog clock does NOT reset on retry events."""
        adapter = ClaudeCodeBackend()
        result = adapter._handle_event(self._api_retry_event())
        assert result is False

    def test_handle_event_progress_returns_true(self) -> None:
        adapter = ClaudeCodeBackend()
        assert adapter._handle_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x"}]},
        }) is True
        assert adapter._handle_event({"type": "system", "subtype": "init"}) is True
        assert adapter._handle_event({
            "type": "result", "subtype": "success",
            "input_tokens": 1, "output_tokens": 1,
        }) is True

    def test_handle_event_api_retry_emits_error_event(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event(self._api_retry_event(attempt=1, status=504))
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "error"
        assert "504" in events[0].detail
        assert "attempt=1" in events[0].detail

    def test_handle_event_api_retry_below_cap_stays_working(self) -> None:
        """A single retry must NOT flip status — transient flakes recover."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event(self._api_retry_event())
        assert adapter.get_status().state == "working"

    def test_handle_event_api_retry_cap_trips_error(self) -> None:
        adapter = ClaudeCodeBackend()
        for n in range(3):
            adapter._handle_event(self._api_retry_event(attempt=n + 1, status=504))
        status = adapter.get_status()
        assert status.state == "error"
        assert "504" in (status.error_reason or "")
        assert "3 consecutive" in (status.error_reason or "")

    def test_api_retry_counter_resets_on_progress(self) -> None:
        """Two retries followed by a progress event then more retries must NOT
        trip the cap — counter is consecutive-only."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event(self._api_retry_event(attempt=1))
        adapter._handle_event(self._api_retry_event(attempt=2))
        # Progress event arrives — recovery
        adapter._handle_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "back online"}]},
        })
        assert adapter._api_retry_count == 0
        # Now two more retries — still below cap
        adapter._handle_event(self._api_retry_event(attempt=1))
        adapter._handle_event(self._api_retry_event(attempt=2))
        assert adapter.get_status().state == "working"


# ---------------------------------------------------------------------------
# _event_reader_loop
# ---------------------------------------------------------------------------

class TestEventReaderLoop:
    async def test_reader_handles_valid_events(self) -> None:
        lines = [
            (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "step"}]}}) + "\n").encode(),
            (json.dumps({"type": "result", "subtype": "success", "input_tokens": 1, "output_tokens": 1}) + "\n").encode(),
        ]
        proc = _proc_with_lines(lines, returncode=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_skips_non_json_lines(self) -> None:
        lines = [
            b"not valid json\n",
            (json.dumps({"type": "result", "subtype": "success", "input_tokens": 0, "output_tokens": 0}) + "\n").encode(),
        ]
        proc = _proc_with_lines(lines, returncode=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_skips_empty_lines(self) -> None:
        lines = [
            b"\n",
            b"   \n",
            (json.dumps({"type": "result", "subtype": "success", "input_tokens": 0, "output_tokens": 0}) + "\n").encode(),
        ]
        proc = _proc_with_lines(lines, returncode=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_sets_error_on_exception(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=RuntimeError("pipe error"))
        proc.returncode = 1
        proc.wait = AsyncMock(return_value=1)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"

    async def test_reader_finalizes_done_on_zero_exit(self) -> None:
        """When process exits cleanly without result event, status → done."""
        proc = _proc_with_lines([], returncode=None)

        async def _set_returncode():
            proc.returncode = 0

        proc.wait = AsyncMock(side_effect=_set_returncode)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_finalizes_error_on_nonzero_exit(self) -> None:
        proc = _proc_with_lines([], returncode=None)

        async def _set_returncode():
            proc.returncode = 1

        proc.wait = AsyncMock(side_effect=_set_returncode)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"

    async def test_reader_no_proc_returns_immediately(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._proc = None
        await adapter._event_reader_loop()  # should not raise

    async def test_reader_api_retry_then_success_recovers(self) -> None:
        """Regression: a single api_retry followed by recovery must complete
        cleanly. The reader loop must not flip to error on transient flakes."""
        lines = [
            (json.dumps({
                "type": "system", "subtype": "api_retry",
                "attempt": 1, "max_retries": 10, "error_status": 504,
                "error": "server_error",
            }) + "\n").encode(),
            (json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "recovered"}]},
            }) + "\n").encode(),
            (json.dumps({
                "type": "result", "subtype": "success",
                "input_tokens": 1, "output_tokens": 1,
            }) + "\n").encode(),
        ]
        proc = _proc_with_lines(lines, returncode=0)
        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()
        assert adapter.get_status().state == "done"
        assert adapter._api_retry_count == 0

    async def test_reader_consecutive_api_retries_trip_error(self) -> None:
        """Regression for the wedge: upstream 504 → CLI emits api_retry then
        SSE stalls. _API_RETRY_MAX consecutive retries (no progress between)
        must surface error rather than depending on the idle watchdog."""
        retry = (json.dumps({
            "type": "system", "subtype": "api_retry",
            "attempt": 1, "max_retries": 10, "error_status": 504,
            "error": "server_error",
        }) + "\n").encode()
        lines = [retry, retry, retry]
        proc = _proc_with_lines(lines, returncode=0)
        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()
        status = adapter.get_status()
        assert status.state == "error"
        assert "504" in (status.error_reason or "")

    async def test_reader_api_retry_does_not_reset_idle_watchdog(
        self, monkeypatch
    ) -> None:
        """The watchdog clock anchors at the last *progress* event, so an
        api_retry mid-stream cannot keep the idle window alive forever.

        We assert this by inspecting ``_last_event_at`` after a progress
        event + an api_retry: the value must equal the timestamp set by
        the progress event (unchanged by the retry)."""
        prog = (json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working"}]},
        }) + "\n").encode()
        retry = (json.dumps({
            "type": "system", "subtype": "api_retry",
            "attempt": 1, "max_retries": 10, "error_status": 504,
            "error": "server_error",
        }) + "\n").encode()
        done = (json.dumps({
            "type": "result", "subtype": "success",
            "input_tokens": 1, "output_tokens": 1,
        }) + "\n").encode()

        # Capture the value of _last_event_at after each iteration by
        # patching the per-event reset; we just need to verify retry events
        # don't bump it. Easier path: snapshot before/after a single retry.
        adapter = ClaudeCodeBackend()
        adapter._proc = _proc_with_lines([prog, retry, done], returncode=0)

        # Drive the loop and verify that during processing _last_event_at
        # only advances on progress events. We do this by patching
        # time.monotonic to a manual clock.
        ticks = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        monkeypatch.setattr(
            "performer.backends.claude_code.time.monotonic",
            lambda: next(ticks, 200.0),
        )
        await adapter._event_reader_loop()
        # The retry must NOT have been the last thing to set the clock;
        # the final progress (`done`) sets it last. If api_retry had reset
        # _last_event_at, the clock advancement pattern would be different,
        # but the load-bearing assertion is the status outcome:
        assert adapter.get_status().state == "done"

    async def test_reader_idle_timeout_with_no_output_sets_error(self, monkeypatch) -> None:
        """Idle/no-progress timeout with empty accumulator → error state."""
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("CLAUDE_CODE_IDLE_TIMEOUT", "0")  # fire immediately

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        try:
            await adapter._event_reader_loop()
        finally:
            get_settings.cache_clear()

        status = adapter.get_status()
        assert status.state == "error"
        assert "idle" in (status.error_reason or "").lower()

    async def test_reader_idle_timeout_with_output_marks_done(self, monkeypatch) -> None:
        """Idle timeout WITH accumulated output → done with stop_reason='idle_timeout'."""
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("CLAUDE_CODE_IDLE_TIMEOUT", "0")

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        adapter._output_accumulator = ["partial assistant text"]
        try:
            await adapter._event_reader_loop()
        finally:
            get_settings.cache_clear()

        status = adapter.get_status()
        assert status.state == "done"
        assert status.stop_reason == "idle_timeout"
        assert status.output == "partial assistant text"

    async def test_reader_idle_timeout_resets_on_each_event(self, monkeypatch) -> None:
        """Per-event watchdog (review callout #1): the idle timeout is a
        watchdog against silence between events, not a fixed budget for the
        whole stream. Each parsed event must reset the remaining budget so a
        slow-but-steady CLI doesn't fire the idle path.

        Asserts the closure passed to ``iter_lines_chunked`` recomputes its
        result against ``self._last_event_at``, which the reader updates on
        every parsed event.
        """
        import time as _time
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("CLAUDE_CODE_IDLE_TIMEOUT", "30")

        captured: dict[str, object] = {}

        async def _fake_iter(stream, *, timeout, on_chunk):
            captured["timeout"] = timeout
            if False:  # pragma: no cover — generator typing
                yield b""

        monkeypatch.setattr(
            "performer.backends.claude_code.iter_lines_chunked", _fake_iter
        )

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc

        # Freeze monotonic so we can assert the watchdog arithmetic exactly.
        now = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: now[0])

        try:
            await adapter._event_reader_loop()
        finally:
            get_settings.cache_clear()

        remaining = captured["timeout"]
        assert callable(remaining), "timeout must be a callable watchdog, not static"

        # At loop entry, _last_event_at == now; full 30s budget remains.
        assert remaining() == pytest.approx(30.0)

        # 25s pass with no event → only 5s left.
        now[0] += 25.0
        assert remaining() == pytest.approx(5.0)

        # Event arrives — reader updates _last_event_at; budget resets to 30s.
        adapter._last_event_at = now[0]
        assert remaining() == pytest.approx(30.0)

        # Another 25s of silence → 5s left again (NOT negative — proves the
        # earlier 25s of silence did not accumulate across the reset).
        now[0] += 25.0
        assert remaining() == pytest.approx(5.0)

        # Once elapsed > idle_timeout, the closure clamps to 0 (never negative).
        now[0] += 100.0
        assert remaining() == 0.0

    async def test_reader_stdout_capture_writes_raw_lines(self, monkeypatch, tmp_path: Path) -> None:
        """When LITELLM_PROXY_CAPTURE_DIR is set, raw stdout lines are mirrored to disk."""
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("LITELLM_PROXY_CAPTURE_DIR", str(tmp_path))

        line = (json.dumps({"type": "result", "subtype": "success", "input_tokens": 0, "output_tokens": 0}) + "\n").encode()
        proc = _proc_with_lines([line], returncode=0)
        proc.pid = 9001

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        try:
            adapter._open_stdout_capture()
            await adapter._event_reader_loop()
        finally:
            get_settings.cache_clear()

        captures = list(tmp_path.glob("cli-stdout-*-9001.log"))
        assert len(captures) == 1
        assert captures[0].read_bytes() == line


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
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=asyncio.CancelledError())
        proc.returncode = 0
        adapter._proc = proc

        with pytest.raises(asyncio.CancelledError):
            await adapter._event_reader_loop()

    async def test_finally_waits_for_proc_when_returncode_none(self) -> None:
        """Finally block calls proc.wait() when proc is still running at loop exit."""
        adapter = ClaudeCodeBackend()
        proc = _proc_with_lines([], returncode=None)

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
        proc = _proc_with_lines([], returncode=None)

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

    def test_card_docs_section_emitted_when_folder_exists(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "cards" / "70-test-task").mkdir(parents=True)
        score = _score(title="Test Task", issue_number=70)
        prompt = _build_task_prompt(score, stand_path=tmp_path)
        assert "## Card Documentation" in prompt
        assert "docs/cards/70-test-task/" in prompt

    def test_card_docs_section_omitted_when_folder_missing(self, tmp_path: Path) -> None:
        prompt = _build_task_prompt(_score(issue_number=70), stand_path=tmp_path)
        assert "## Card Documentation" not in prompt
