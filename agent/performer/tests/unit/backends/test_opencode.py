"""Unit tests for OpenCodeAdapter."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


from performer.backends.opencode import OpenCodeAdapter
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


class TestOpenCodeAdapterStart:
    async def test_start_launches_subprocess_and_sends_task(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.pid = 999
        proc.stdin = AsyncMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdout = AsyncMock()
        async def _empty_reader():
            return
            yield  # make it an async generator

        proc.stdout.__aiter__ = lambda self: _empty_reader()

        adapter = OpenCodeAdapter()
        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await adapter.start(_stand(tmp_path), _score())

        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0].decode()
        msg = json.loads(written.strip())
        assert msg["type"] == "message.create"
        assert "Task: T" in msg["parts"][0]["text"]


class TestOpenCodeAdapterGetStatus:
    def test_initial_status_is_working(self) -> None:
        adapter = OpenCodeAdapter()
        assert adapter.get_status().state == "working"

    async def test_session_idle_transitions_to_done(self, tmp_path: Path) -> None:
        async def _fake_reader():
            yield json.dumps({"type": "session.idle"}).encode() + b"\n"

        proc = MagicMock()
        proc.pid = 1
        proc.stdin = AsyncMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _fake_reader()

        adapter = OpenCodeAdapter()
        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await adapter.start(_stand(tmp_path), _score())

        # Let the reader task process the event
        await asyncio.sleep(0.05)
        assert adapter.get_status().state == "done"

    async def test_session_error_transitions_to_error(self, tmp_path: Path) -> None:
        async def _fake_reader():
            yield json.dumps({"type": "session.error", "error": {"message": "boom"}}).encode() + b"\n"

        proc = MagicMock()
        proc.pid = 1
        proc.stdin = AsyncMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _fake_reader()

        adapter = OpenCodeAdapter()
        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await adapter.start(_stand(tmp_path), _score())

        await asyncio.sleep(0.05)
        status = adapter.get_status()
        assert status.state == "error"
        assert "boom" in (status.error_reason or "")


class TestOpenCodeAdapterRelayFeedback:
    async def test_relay_feedback_writes_nd_json_to_stdin(self, tmp_path: Path) -> None:
        written_data: list[bytes] = []

        proc = MagicMock()
        proc.pid = 1
        proc.stdin = AsyncMock()
        proc.stdin.write = MagicMock(side_effect=lambda b: written_data.append(b))
        proc.stdin.drain = AsyncMock()
        async def _empty_reader():
            return
            yield  # make it an async generator

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _empty_reader()

        adapter = OpenCodeAdapter()
        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await adapter.start(_stand(tmp_path), _score())

        written_data.clear()
        await adapter.relay_feedback("please use GitHub Actions")

        assert len(written_data) == 1
        msg = json.loads(written_data[0].decode().strip())
        assert msg["type"] == "message.create"
        assert "GitHub Actions" in msg["parts"][0]["text"]


class TestOpenCodeAdapterStop:
    async def test_stop_kills_process_group(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.pid = 1234
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        async def _empty_reader():
            return
            yield  # make it an async generator

        proc.stdout = MagicMock()
        proc.stdout.__aiter__ = lambda self: _empty_reader()
        proc.wait = AsyncMock(return_value=None)

        adapter = OpenCodeAdapter()
        with patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await adapter.start(_stand(tmp_path), _score())

        with (
            patch("performer.backends.opencode.os.getpgid", return_value=1234),
            patch("performer.backends.opencode.os.killpg") as mock_killpg,
        ):
            await adapter.stop()

        mock_killpg.assert_called_once()
