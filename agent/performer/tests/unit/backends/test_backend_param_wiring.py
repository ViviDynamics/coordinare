"""Verify that start() keyword arguments are actually forwarded to the
underlying process or API call — not just accepted and silently dropped.

Each test patches at the lowest practical boundary so the assertion is tight:
  - ClaudeCodeBackend → asyncio.create_subprocess_exec args list
  - OpenCodeAdapter   → httpx POST /session request body
  - CodexBackend      → _rpc("thread/start", ...) params dict
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.codex import CodexBackend
from performer.backends.opencode import OpenCodeAdapter
from performer.models import Score, Stand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="main", git_env={})


def _make_score(**overrides) -> Score:
    base = dict(
        title="test task",
        repo_url="https://github.com/example/repo",
        branch="main",
    )
    return Score(**(base | overrides))


def _fake_proc():
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 12345
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(return_value=b"")
    # Reader loops read stdout.read(n) — must return EOF (b"") immediately,
    # otherwise the unconfigured AsyncMock returns a truthy MagicMock and the
    # reader spins forever.
    proc.stdout.read = AsyncMock(return_value=b"")
    return proc


# ---------------------------------------------------------------------------
# ClaudeCodeBackend
# ---------------------------------------------------------------------------

class TestClaudeCodeParamWiring:
    """model and max_tokens must reach the claude CLI invocation."""

    @pytest.mark.asyncio
    async def test_model_passed_to_cli(self, tmp_path):
        backend = ClaudeCodeBackend()
        score = _make_score()
        stand = _make_stand(tmp_path)

        captured_args = []

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            return _fake_proc()

        with patch("performer.backends.claude_code.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await backend.start(stand, score, model="claude-opus-4-7")

        assert "--model" in captured_args
        assert "claude-opus-4-7" in captured_args

    @pytest.mark.asyncio
    async def test_max_tokens_passed_to_cli(self, tmp_path):
        backend = ClaudeCodeBackend()
        score = _make_score()
        stand = _make_stand(tmp_path)

        captured_args = []

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            return _fake_proc()

        with patch("performer.backends.claude_code.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await backend.start(stand, score, max_tokens=4096)

        assert "--max-tokens" in captured_args
        assert "4096" in captured_args

    @pytest.mark.asyncio
    async def test_no_model_flag_when_omitted(self, tmp_path):
        backend = ClaudeCodeBackend()
        score = _make_score()
        stand = _make_stand(tmp_path)

        captured_args = []

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            return _fake_proc()

        with patch("performer.backends.claude_code.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await backend.start(stand, score)

        assert "--model" not in captured_args
        assert "--max-tokens" not in captured_args


# ---------------------------------------------------------------------------
# OpenCodeAdapter
# ---------------------------------------------------------------------------

class TestOpenCodeParamWiring:
    """model and max_tokens must appear in the POST /session body."""

    async def _patched_start(self, tmp_path, model=None, max_tokens=None):
        """Run OpenCodeAdapter.start() with all subprocess/network mocked out."""
        backend = OpenCodeAdapter(executable="opencode", adapter_name="opencode")
        score = _make_score()
        stand = _make_stand(tmp_path)

        posted_bodies = []

        async def fake_post(path, *, json=None, **kwargs):
            posted_bodies.append((path, json))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"id": "sess-123"})
            return resp

        fake_client = AsyncMock()
        fake_client.post.side_effect = fake_post
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("performer.backends.opencode.asyncio.create_subprocess_exec", return_value=_fake_proc()),
            patch.object(backend, "_drain_logs", AsyncMock()),
            patch.object(backend, "_event_reader_loop", AsyncMock()),
            patch.object(backend, "_wait_for_ready", AsyncMock()),
            patch("performer.backends.opencode.httpx.AsyncClient", return_value=fake_client),
        ):
            await backend.start(stand, score, model=model, max_tokens=max_tokens)

        return posted_bodies

    @pytest.mark.asyncio
    async def test_model_in_session_body(self, tmp_path):
        bodies = await self._patched_start(tmp_path, model="gpt-4o")
        session_body = next(body for path, body in bodies if path == "/session")
        assert session_body is not None
        assert session_body.get("modelID") == "gpt-4o"

    @pytest.mark.asyncio
    async def test_max_tokens_in_session_body(self, tmp_path):
        bodies = await self._patched_start(tmp_path, max_tokens=8192)
        session_body = next(body for path, body in bodies if path == "/session")
        assert session_body is not None
        assert session_body.get("maxTokens") == 8192

    @pytest.mark.asyncio
    async def test_empty_session_body_when_omitted(self, tmp_path):
        bodies = await self._patched_start(tmp_path)
        session_body = next((body for path, body in bodies if path == "/session"), None)
        # When neither model nor max_tokens is set, body is None (not an empty dict)
        assert session_body is None


# ---------------------------------------------------------------------------
# CodexBackend
# ---------------------------------------------------------------------------

class TestCodexParamWiring:
    """model must appear in the thread/start RPC params."""

    @pytest.mark.asyncio
    async def test_model_in_thread_params(self, tmp_path):
        backend = CodexBackend()
        score = _make_score()
        stand = _make_stand(tmp_path)

        rpc_calls: list[tuple] = []

        async def fake_rpc(method, params=None):
            rpc_calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/start":
                return {"thread": {"id": "t-1"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            return {}

        mock_ws_session = MagicMock()
        mock_ws_session.ws_connect = AsyncMock(return_value=MagicMock())

        with (
            patch("performer.backends.codex.asyncio.create_subprocess_exec", return_value=_fake_proc()),
            patch.object(backend, "_drain_logs", AsyncMock()),
            patch.object(backend, "_recv_loop", AsyncMock()),
            patch.object(backend, "_wait_for_ready", AsyncMock()),
            patch.object(backend, "_rpc", side_effect=fake_rpc),
            patch.object(backend, "_notify", AsyncMock()),
            patch("performer.backends.codex.aiohttp.ClientSession", return_value=mock_ws_session),
        ):
            await backend.start(stand, score, model="o3")

        thread_start = next(p for m, p in rpc_calls if m == "thread/start")
        assert thread_start.get("model") == "o3"

    @pytest.mark.asyncio
    async def test_no_model_key_when_omitted(self, tmp_path):
        backend = CodexBackend()
        score = _make_score()
        stand = _make_stand(tmp_path)

        rpc_calls: list[tuple] = []

        async def fake_rpc(method, params=None):
            rpc_calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/start":
                return {"thread": {"id": "t-1"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            return {}

        mock_ws_session = MagicMock()
        mock_ws_session.ws_connect = AsyncMock(return_value=MagicMock())

        with (
            patch("performer.backends.codex.asyncio.create_subprocess_exec", return_value=_fake_proc()),
            patch.object(backend, "_drain_logs", AsyncMock()),
            patch.object(backend, "_recv_loop", AsyncMock()),
            patch.object(backend, "_wait_for_ready", AsyncMock()),
            patch.object(backend, "_rpc", side_effect=fake_rpc),
            patch.object(backend, "_notify", AsyncMock()),
            patch("performer.backends.codex.aiohttp.ClientSession", return_value=mock_ws_session),
        ):
            await backend.start(stand, score)

        thread_start = next(p for m, p in rpc_calls if m == "thread/start")
        assert "model" not in thread_start
