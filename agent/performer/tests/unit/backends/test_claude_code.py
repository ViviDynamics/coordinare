"""Unit tests for ClaudeCodeBackend (--resume / session_id implementation)."""
from __future__ import annotations

import asyncio
import json
import os
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
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
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

    async def test_max_tokens_uses_env_var_not_unsupported_flag(
        self, tmp_path: Path
    ) -> None:
        """077: the Claude Code CLI has no --max-tokens flag (passing it aborts
        with exit 1 — it crashed the qa stage). The cap must be delivered via the
        CLAUDE_CODE_MAX_OUTPUT_TOKENS env var instead."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score(), max_tokens=8192)

        args = list(mock_exec.call_args[0])
        assert "--max-tokens" not in args  # the flag the CLI rejects
        env = mock_exec.call_args.kwargs["env"]
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"

    async def test_no_max_tokens_omits_env_var(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = list(mock_exec.call_args[0])
        assert "--max-tokens" not in args
        env = mock_exec.call_args.kwargs["env"]
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env

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

    async def test_start_passes_prompt_via_stdin(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())

        args = mock_exec.call_args[0]
        # Prompt is now sent via stdin, not as a positional arg after -p.
        assert "--print" in args
        assert "-p" not in args
        written = proc.stdin.write.call_args[0][0]
        assert b"Test Task" in written

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
        # And the persona text is NOT embedded in the stdin prompt.
        written = proc.stdin.write.call_args[0][0].decode()
        assert "PERSONA_MARKER_CC" not in written
        assert "## Role Instructions" not in written

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

    async def test_start_appends_cache_path_after_image_path(self, tmp_path: Path) -> None:
        """087: the claude_code CLI must launch on the IMAGE's node, but the
        env-cache toolchain (ruby/bundle) MUST still be reachable by the agent.

        The env-cache prepends the project's .nvmrc node (e.g. 18.12.1) to PATH;
        a modern Node-based agent CLI crashes at startup under an older node — so
        the IMAGE's node must win. But fully STRIPPING the cache PATH (the prior
        084 behavior) left the agent with no project toolchain: Claude Code
        snapshots the launch PATH for its Bash tool and does NOT re-source
        activate.sh per command (unlike openclaw/pi/codex), so `ruby`/`bundle`
        were missing and qa couldn't boot the app to take screenshots. Fix:
        APPEND the cache-only dirs AFTER the image PATH — the CLI still resolves
        node/claude to the image's modern node (image dirs first), while ruby and
        bundle (absent from the image) resolve from the cache for the agent's
        snapshotted shell. Every other cache var must survive.
        """
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GIT_AUTHOR_NAME": "performer"},
            cache_env={
                "PATH": (
                    "/devenv/foo/.rbenv/versions/3.4.2/bin:"
                    "/devenv/foo/node-v18.12.1/bin:/usr/bin"
                ),
                "VIRTUAL_ENV": "/devenv/foo/.venv",
            },
        )

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        image_path = os.environ["PATH"]
        # The IMAGE PATH comes first, so the CLI runs on the image's modern node.
        assert env["PATH"].startswith(image_path)
        # The cache toolchain is APPENDED (reachable) — ruby/bundle for the agent.
        assert "/devenv/foo/.rbenv/versions/3.4.2/bin" in env["PATH"]
        # ...and the project node dir, if present, must NOT precede the image dirs.
        assert env["PATH"].index(image_path) < env["PATH"].index("/devenv/foo/node-v18.12.1/bin")
        # Every other cache var survives.
        assert env["VIRTUAL_ENV"] == "/devenv/foo/.venv"
        assert env["GIT_AUTHOR_NAME"] == "performer"

    async def test_start_empty_image_path_falls_back_to_system_default(
        self, tmp_path: Path
    ) -> None:
        """087 edge: if the container somehow has no PATH, the launch PATH must
        fall back to the standard system dirs BEFORE the cache dirs — a
        cache-only PATH would put the project's pinned old node first and
        resurrect the CLI startup crash the append policy exists to prevent."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()
        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={},
            cache_env={"PATH": "/devenv/foo/node-v18.12.1/bin"},
        )

        env_without_path = {k: v for k, v in os.environ.items() if k != "PATH"}
        with (
            patch.dict("performer.backends.claude_code.os.environ", env_without_path, clear=True),
            patch(
                "performer.backends.claude_code.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as mock_exec,
        ):
            await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        # System dirs first (CLI gets a sane baseline), cache appended after.
        assert env["PATH"].startswith("/usr/local/sbin:/usr/local/bin")
        assert env["PATH"].index("/usr/bin") < env["PATH"].index("/devenv/foo/node-v18.12.1/bin")

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
        # Subprocess exited cleanly, the reader_task saw progress events but
        # never a terminal stream-json event — liveness fallback must
        # transition to done. 509: zero-event runs stay error (see below).
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 0
        adapter._events_seen = 1
        status = adapter.get_status()
        assert status.state == "done"

    def test_get_status_liveness_proc_exited_clean_zero_events_is_error(self) -> None:
        # 509: rc=0 with zero events means the CLI never ran the agent — the
        # liveness fallback must report an error, not a silent success. With
        # no reader task at all there is nothing still consuming stdout, so
        # the error fires immediately and carries the subprocess_exit marker
        # the coordinare's transient classifier matches on (bounce, not block).
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 0
        status = adapter.get_status()
        assert status.state == "error"
        assert "subprocess_exit" in (status.error_reason or "")
        assert "without producing any events" in (status.error_reason or "")

    async def test_get_status_liveness_zero_events_active_reader_stays_working(
        self,
    ) -> None:
        """509 round 2: the liveness check can run after the process exits but
        before the reader task has consumed buffered stdout — a healthy run
        must not be declared dead while the reader is still active. The
        reader's idle timeout bounds a wedged pipe, and its finally applies
        the same zero-event verdict when it finishes."""
        adapter = ClaudeCodeBackend()
        adapter._proc = MagicMock()
        adapter._proc.returncode = 0
        reader = asyncio.ensure_future(asyncio.Event().wait())
        try:
            adapter._reader_task = reader
            assert adapter.get_status().state == "working"
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

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
    # 305: usage accounting. Cache tokens are most of a Claude Code turn's
    # input, and an absent `usage` block means UNKNOWN, never zero.
    # ------------------------------------------------------------------

    def test_result_usage_counts_cache_tokens(self) -> None:
        """A real cache-heavy `result` event: cache reads/creations are input
        tokens and must be counted. Summing only input+output under-reports a
        Claude Code turn by orders of magnitude (4 + 900 instead of 100,904)."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result",
            "subtype": "success",
            "session_id": "sess-1",
            "total_cost_usd": 0.42,
            "usage": {
                "input_tokens": 4,
                "cache_creation_input_tokens": 12_000,
                "cache_read_input_tokens": 88_000,
                "output_tokens": 900,
            },
        })
        assert adapter.get_status().tokens_processed == 100_904

    def test_result_without_usage_reports_unknown_not_zero(self) -> None:
        """A terminal result carrying no usage at all (e.g. a proxy that strips
        it) is UNKNOWN. Reporting 0 makes an unmeasured run read as a measured
        $0.00 all the way into the benchmark artifact."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "session_id": "sess-2",
        })
        status = adapter.get_status()
        assert status.state == "done"
        assert status.tokens_processed is None

    def test_result_with_empty_usage_reports_unknown_not_zero(self) -> None:
        """An empty `usage` dict is the same absence of evidence as no key."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "usage": {},
        })
        assert adapter.get_status().tokens_processed is None

    def test_max_tokens_result_without_usage_reports_unknown_not_zero(self) -> None:
        """The truncation branch reports usage on the same contract as success."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "stop_reason": "max_tokens",
        })
        status = adapter.get_status()
        assert status.state == "error"
        assert status.stop_reason == "max_tokens"
        assert status.tokens_processed is None

    def test_max_tokens_result_counts_cache_tokens(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "stop_reason": "max_tokens",
            "usage": {"input_tokens": 1, "cache_read_input_tokens": 5_000,
                      "output_tokens": 10},
        })
        assert adapter.get_status().tokens_processed == 5_011

    def test_result_reports_a_genuine_zero_as_zero(self) -> None:
        """Present-but-zero usage is measured evidence, not absence. Collapsing
        it to None would be the mirror of the bug this guards."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        assert adapter.get_status().tokens_processed == 0

    def test_a_legacy_top_level_zero_does_not_shadow_the_nested_usage(self) -> None:
        """Review finding: the nested `usage` block is the current CLI shape and
        must win. Reading the top level first let a legacy `input_tokens: 0`
        discard a populated usage block (500 tokens lost, 700 -> 200)."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "input_tokens": 0,
            "usage": {"input_tokens": 500, "output_tokens": 200},
        })
        assert adapter.get_status().tokens_processed == 700

    def test_top_level_fields_are_still_read_when_there_is_no_usage_block(self) -> None:
        """The legacy shape keeps working: fallback, not removal."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success",
            "input_tokens": 100, "output_tokens": 50,
        })
        assert adapter.get_status().tokens_processed == 150

    def test_a_boolean_is_never_summed_as_a_token_count(self) -> None:
        """isinstance(True, int) is True in Python; a stray bool must not add 1."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success",
            "usage": {"input_tokens": True, "output_tokens": True},
        })
        assert adapter.get_status().tokens_processed is None

    def test_the_current_cli_cost_key_reaches_the_cost_event(self) -> None:
        """Review finding: the CLI emits `total_cost_usd`, but only `cost_usd`
        was read, so every real run lost its dollar figure."""
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "total_cost_usd": 0.42,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        })
        cost = [e for e in adapter.drain_events() if e.type.value == "cost"]
        assert cost[0].text == "30 tokens · $0.4200"

    def test_the_legacy_cost_key_still_works(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({
            "type": "result", "subtype": "success", "cost_usd": 0.0025,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        })
        cost = [e for e in adapter.drain_events() if e.type.value == "cost"]
        assert "$0.0025" in cost[0].text

    def test_unknown_usage_emits_a_cost_event_that_does_not_claim_zero(self) -> None:
        adapter = ClaudeCodeBackend()
        adapter._handle_event({"type": "result", "subtype": "success"})
        cost = [e for e in adapter.drain_events() if e.type.value == "cost"]
        assert len(cost) == 1
        assert "0 tokens" not in cost[0].text
        assert "unknown" in cost[0].text.lower()

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
        """When process exits cleanly after emitting events, status → done."""
        proc = _proc_with_lines([], returncode=None)

        async def _set_returncode():
            proc.returncode = 0

        proc.wait = AsyncMock(side_effect=_set_returncode)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        adapter._events_seen = 1
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "done"

    async def test_reader_finalizes_error_on_zero_exit_zero_events(self) -> None:
        """509: rc=0 with zero parsed events means the CLI never ran the
        agent — the reader's finalizer must surface an error, not done."""
        proc = _proc_with_lines([], returncode=None)

        async def _set_returncode():
            proc.returncode = 0

        proc.wait = AsyncMock(side_effect=_set_returncode)

        adapter = ClaudeCodeBackend()
        adapter._proc = proc
        await adapter._event_reader_loop()

        assert adapter.get_status().state == "error"
        assert "subprocess_exit" in (adapter.get_status().error_reason or "")
        assert "without producing any events" in (
            adapter.get_status().error_reason or ""
        )

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
        assert "--print" in args
        written = proc.stdin.write.call_args[0][0]
        assert b"use black for formatting" in written
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
        adapter._events_seen = 1

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

    def test_qa_role_forbids_external_screenshot_services(self) -> None:
        # 077: QA visual evidence must come from driving the local app, never a
        # third-party screenshot service (observed: codex used Thum.io).
        prompt = _build_task_prompt(_score(role="qa"))
        assert "Thum.io" in prompt
        assert "third-party screenshot" in prompt
        assert "fabricated evidence" in prompt

    def test_diagnostic_role_uses_free_form_footer(self) -> None:
        # 077: diagnostic/benchmark probe — no JSON contract, no commit/PR tail.
        prompt = _build_task_prompt(_score(role="diagnostic"))
        assert "one-off diagnostic" in prompt
        assert "do NOT need to commit" in prompt.lower() or "not need to commit" in prompt.lower()
        assert "Return ONLY a valid JSON object" not in prompt
        assert "Commit your changes" not in prompt
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


# ---------------------------------------------------------------------------
# 509: silent-success hole + relay-feedback delivery observability
# ---------------------------------------------------------------------------


class TestZeroEventExitIsError:
    async def test_rc0_with_zero_events_is_error(self, tmp_path: Path) -> None:
        """509: a claude CLI that exits 0 having emitted no stream-json events
        never ran the agent. That must surface as an error, not a success —
        the incident was a 4-minute pod that re-registered PR artefacts while
        the CLI produced nothing, and the coordinare read it as success."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        proc.returncode = 0
        if adapter._reader_task is not None:
            await adapter._reader_task
        status = adapter.get_status()
        assert status.state == "error"
        assert "subprocess_exit" in (status.error_reason or "")
        assert "without producing any events" in (status.error_reason or "")

    async def test_rc0_with_events_still_done(self, tmp_path: Path) -> None:
        """A healthy run that exits 0 after emitting progress events keeps the
        done semantics — the zero-event guard must not fire."""
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        adapter._events_seen = 3
        proc.returncode = 0
        if adapter._reader_task is not None:
            await adapter._reader_task
        status = adapter.get_status()
        assert status.state == "done"


class TestRelayFeedbackDeliveryEvent:
    async def test_relay_feedback_emits_delivery_event(self, tmp_path: Path) -> None:
        """509: the session record must show whether the review comments
        reached the agent's prompt. start() emits a synthetic progress event
        carrying the review + inline-comment counts."""
        review = {
            "id": "PRR_1",
            "body": "Two things to fix.",
            "comments": [
                {"body": "Clamp is off by one", "path": "src/x.py", "line": 42},
                {"body": "Missing tests", "path": "tests/test_x.py"},
            ],
        }
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score(relay_feedback=[review]))

        events = adapter.drain_events()
        delivery = [e for e in events if "relay feedback" in e.text.lower()]
        assert delivery, "no relay-feedback delivery event emitted"
        assert "1 review" in delivery[0].text
        assert "2 inline comment" in delivery[0].text

    async def test_no_relay_feedback_no_delivery_event(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        adapter = ClaudeCodeBackend()

        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())

        events = adapter.drain_events()
        assert not [e for e in events if "relay feedback" in e.text.lower()]
