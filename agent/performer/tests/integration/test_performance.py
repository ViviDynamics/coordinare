"""Integration tests for performer handlers and workspace cleanup.

Uses:
- A real local git repository (tmp_path) as the stand
- OpenCodeAdapter.start patched to use mock_opencode_serve.py (HTTP) or
  an in-process httpx mock (respx)
- respx to mock GitHub API PR creation
- Exercises handle_dispatch → handle_status → cleanup_stand directly
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from performer.backends.opencode import OpenCodeAdapter
from performer.config import Settings, get_settings
from performer.main import handle_dispatch, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage
from performer.workspace import cleanup_stand


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


def _github_mocks() -> None:
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
    respx.get("https://api.github.com/repos/org/repo/commits/abc123/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": []})
    )


def _make_patched_start(stand_override: Stand) -> object:
    """Return an OpenCodeAdapter.start replacement that uses respx-mocked HTTP."""

    async def _patched_start(self: OpenCodeAdapter, stand: Stand, score: Score, *, model: str | None = None) -> None:
        port = 19950
        self._port = port
        self._session_id = "mock-session-1"
        self._workspace_dir = str(stand.path)
        self._status = self._status.__class__(state="working")

        # Set up a real httpx client pointing at the respx-intercepted base URL
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(stand.path)},
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
        )

        # Immediately transition to done (mock the reader task finishing)
        async def _instant_done() -> None:
            await asyncio.sleep(0.05)
            self._status = self._status.__class__(state="done")

        self._reader_task = asyncio.create_task(_instant_done(), name="opencode-event-reader")
        self._proc = None  # no real process

    return _patched_start


class TestFullPerformanceLoop:
    @respx.mock
    @pytest.mark.timeout(30)
    async def test_dispatch_status_pr_opened_and_cleanup(self, tmp_path: Path) -> None:
        """Full loop: dispatch → status polls → pr_opened + stand cleanup."""
        stand_dir = tmp_path / "stand"
        stand_dir.mkdir()

        _github_mocks()

        get_settings.cache_clear()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=30)
        mock_stand = Stand(path=stand_dir, branch="feat/integration-test")

        async def _mock_clone(score: Score) -> Stand:
            return mock_stand

        async def _mock_push(stand: Stand, score: Score) -> None:
            pass

        msg = _make_dispatch_msg()

        with (
            patch("performer.main.clone_repository", new=AsyncMock(side_effect=_mock_clone)),
            patch("performer.main.push_branch", new=AsyncMock(side_effect=_mock_push)),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
            patch.object(OpenCodeAdapter, "start", _make_patched_start(mock_stand)),
        ):
            resp, perf = await handle_dispatch(msg, settings)
            assert resp.status == "accepted"
            session_id = resp.session_id
            assert session_id

            # Poll status until terminal state (max 20 iterations × 0.2s = 4s).
            # Backend done → working (waiting_for_checks) → pr_opened after checks pass.
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

        cleanup_stand(perf.stand)
        assert not stand_dir.exists(), "Stand directory should be deleted after cleanup"

    @respx.mock
    @pytest.mark.timeout(30)
    async def test_backend_done_triggers_pr_and_cleanup(self, tmp_path: Path) -> None:
        """Verify stand is removed after terminal state."""
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
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        ):
            respx.get("https://api.github.com/repos/org/repo/commits/abc123/check-runs").mock(
                return_value=httpx.Response(200, json={"check_runs": []})
            )
            with patch.object(OpenCodeAdapter, "start", _make_patched_start(mock_stand)):
                _, perf = await handle_dispatch(msg, settings)
                await asyncio.sleep(0.5)
                # First poll: backend done → push/PR → waiting_for_checks → working
                status_msg = PerformerMessage(action="status", session_id=perf.session_id)
                interim = await handle_status(status_msg, perf)
                assert interim.status == "working"
                assert perf.state == "waiting_for_checks"
                # Second poll: checks pass (empty list) → pr_opened
                final_resp = await handle_status(status_msg, perf)

        assert final_resp.status == "pr_opened"
        cleanup_stand(perf.stand)
        assert not stand_dir.exists()
