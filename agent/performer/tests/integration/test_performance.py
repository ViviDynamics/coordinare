"""Integration tests for performer handlers and workspace cleanup with mock opencode ACP.

Uses:
- A real local git repository (tmp_path) as the stand
- mock_opencode_acp.py fixture as the opencode subprocess
- respx to mock GitHub API PR creation
- Exercises handle_dispatch → handle_status → cleanup_stand directly;
  does not exercise the stdin→stdout run_loop (see test_main.py for handler unit tests)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import respx
import httpx

from performer.backends.opencode import OpenCodeAdapter
from performer.config import Settings, get_settings
from performer.main import handle_dispatch, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage
from performer.workspace import cleanup_stand

# Path to the mock opencode ACP script (sibling fixtures/ directory)
_MOCK_ACP = str(Path(__file__).parent.parent / "fixtures" / "mock_opencode_acp.py")


def _make_dispatch_msg() -> PerformerMessage:
    return PerformerMessage(
        action="dispatch",
        payload={
            "title": "Integration test task",
            "description": "Test description",
            "repo_url": "https://github.com/org/repo",
            "branch": "feat/integration-test",
            "github_token": "ghp_integration_test_token",
        },
    )


class TestFullPerformanceLoop:
    @respx.mock
    @pytest.mark.timeout(30)
    async def test_dispatch_status_pr_opened_and_cleanup(self, tmp_path: Path) -> None:
        """Full loop: dispatch → status polls → pr_opened + stand cleanup."""
        stand_dir = tmp_path / "stand"
        stand_dir.mkdir()

        # Mock GitHub API
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(
                201,
                json={
                    "html_url": "https://github.com/org/repo/pull/99",
                    "node_id": "PR_integration_node",
                },
            )
        )

        get_settings.cache_clear()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=30)

        mock_stand = Stand(path=stand_dir, branch="feat/integration-test")

        # Patch clone_repository to return our pre-built stand directory
        # Patch push_branch to no-op (local repo can't push to GitHub)
        # Patch OpenCodeAdapter to use mock_opencode_acp.py instead of `opencode acp`
        async def _mock_clone(score: Score) -> Stand:
            return mock_stand

        async def _mock_push(stand: Stand, score: Score) -> None:
            pass  # skip actual git push in integration test


        async def _patched_start(self: OpenCodeAdapter, stand: Stand, score: Score) -> None:
            """Launch mock_opencode_acp.py instead of `opencode acp`."""
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, _MOCK_ACP,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(stand.path),
            )
            from performer.backends.opencode import _build_task_prompt
            initial_message = {
                "type": "message.create",
                "parts": [{"type": "text", "text": _build_task_prompt(score)}],
            }
            await self._write_nd_json(initial_message)
            self._reader_task = asyncio.create_task(
                self._event_reader_loop(), name="opencode-reader"
            )

        msg = _make_dispatch_msg()

        with (
            patch("performer.main.clone_repository", new=AsyncMock(side_effect=_mock_clone)),
            patch("performer.main.push_branch", new=AsyncMock(side_effect=_mock_push)),
            patch.object(OpenCodeAdapter, "start", _patched_start),
        ):
            resp, perf = await handle_dispatch(msg, settings)
            assert resp.status == "accepted"
            session_id = resp.session_id
            assert session_id

            # Poll status until terminal state (max 20 iterations × 0.2s = 4s)
            final_resp = None
            for _ in range(20):
                status_msg = PerformerMessage(action="status", session_id=session_id)
                final_resp = await handle_status(status_msg, perf)
                if final_resp.status in ("pr_opened", "error"):
                    break
                await asyncio.sleep(0.2)

        assert final_resp is not None
        assert final_resp.status == "pr_opened", f"Expected pr_opened, got: {final_resp.status}"
        assert final_resp.pr_url == "https://github.com/org/repo/pull/99"
        assert final_resp.pr_node_id == "PR_integration_node"

        # Cleanup stand
        cleanup_stand(perf.stand)
        assert not stand_dir.exists(), "Stand directory should be deleted after cleanup"

    @respx.mock
    @pytest.mark.timeout(30)
    async def test_backend_done_triggers_pr_and_cleanup(self, tmp_path: Path) -> None:
        """Verify stand is removed after terminal state regardless of how mock exits."""
        stand_dir = tmp_path / "stand2"
        stand_dir.mkdir()
        mock_stand = Stand(path=stand_dir, branch="feat/x")

        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(
                201,
                json={"html_url": "https://github.com/org/repo/pull/1", "node_id": "N1"},
            )
        )

        get_settings.cache_clear()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=30)

        async def _mock_clone(score: Score) -> Stand:
            return mock_stand

        async def _mock_push(stand: Stand, score: Score) -> None:
            pass

        async def _patched_start(self: OpenCodeAdapter, stand: Stand, score: Score) -> None:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, _MOCK_ACP,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(stand.path),
            )
            await self._write_nd_json({"type": "message.create", "parts": []})
            self._reader_task = asyncio.create_task(
                self._event_reader_loop(), name="opencode-reader"
            )

        msg = PerformerMessage(
            action="dispatch",
            payload={
                "title": "T",
                "repo_url": "https://github.com/org/repo",
                "branch": "feat/x",
                "github_token": "tok",
            },
        )

        with (
            patch("performer.main.clone_repository", new=AsyncMock(side_effect=_mock_clone)),
            patch("performer.main.push_branch", new=AsyncMock(side_effect=_mock_push)),
            patch.object(OpenCodeAdapter, "start", _patched_start),
        ):
            _, perf = await handle_dispatch(msg, settings)
            await asyncio.sleep(0.5)  # let mock process finish
            status_msg = PerformerMessage(action="status", session_id=perf.session_id)
            final_resp = await handle_status(status_msg, perf)

        assert final_resp.status == "pr_opened"
        cleanup_stand(perf.stand)
        assert not stand_dir.exists()
