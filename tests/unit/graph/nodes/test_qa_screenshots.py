"""Tests for qa_screenshots LangGraph node and screenshot_service (055)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes.qa_screenshots import qa_screenshots
from coordinare.graph.state import initial_state
from coordinare.services.screenshot_service import (
    DockerSession,
    QAScreenshotResult,
    capture_screenshots,
    launch_docker_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(workspace_path: Path | None = None, qa_docker_enabled: bool = True):
    state = initial_state()
    state["workspace_path"] = workspace_path
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    class _Config:
        qa_docker_enabled = True
        qa_playwright_image = "mcr.microsoft.com/playwright:v1.44.0-jammy"
        qa_screenshot_timeout_s = 30

    cfg = _Config()
    cfg.qa_docker_enabled = qa_docker_enabled
    state["config"] = cfg
    return state


async def _ok_run(cmd: list[str], timeout: float = 30.0):
    return 0, "container_abc", ""


async def _fail_run(cmd: list[str], timeout: float = 30.0):
    return 1, "", "docker: command not found"


# ---------------------------------------------------------------------------
# screenshot_service unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_docker_env_no_workspace_compose(tmp_path):
    """No compose file present → pull image then wait; app unreachable → None."""
    call_log: list[list[str]] = []

    async def mock_run(cmd: list[str], timeout: float = 30.0):
        call_log.append(cmd)
        if "version" in cmd:
            return 0, "Docker version 24", ""
        if "pull" in cmd:
            return 0, "", ""
        if "curl" in cmd:
            return 1, "", "connection refused"  # app never reachable
        return 1, "", "unexpected"

    with patch("shutil.which", return_value="/usr/bin/docker"):
        result = await launch_docker_env(
            playwright_image="test-image:latest",
            workspace_path=tmp_path,
            app_url="http://localhost:9999",
            timeout_s=2,
            _run_fn=mock_run,
        )

    assert result is None  # app never reachable


@pytest.mark.asyncio
async def test_launch_docker_env_docker_not_found(tmp_path):
    with patch("shutil.which", return_value=None):
        result = await launch_docker_env(
            playwright_image="img",
            workspace_path=tmp_path,
            _run_fn=_ok_run,
        )
    assert result is None


@pytest.mark.asyncio
async def test_launch_docker_env_docker_down(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/docker"):
        result = await launch_docker_env(
            playwright_image="img",
            workspace_path=tmp_path,
            _run_fn=_fail_run,
        )
    assert result is None


@pytest.mark.asyncio
async def test_launch_docker_env_compose_success(tmp_path):
    """Compose file present, compose up succeeds, app reachable."""
    (tmp_path / "docker-compose.yml").write_text("version: '3'")

    async def mock_run(cmd: list[str], timeout: float = 30.0):
        if "version" in cmd:
            return 0, "Docker 24", ""
        if "up" in cmd:
            return 0, "container_xyz\n", ""
        if "curl" in cmd:
            return 0, "", ""
        return 1, "", "unexpected"

    with patch("shutil.which", return_value="/usr/bin/docker"):
        result = await launch_docker_env(
            playwright_image="img",
            workspace_path=tmp_path,
            timeout_s=5,
            _run_fn=mock_run,
        )

    assert result is not None
    assert result.container_id == "container_xyz"
    assert (tmp_path / "qa_screenshots").is_dir()


@pytest.mark.asyncio
async def test_capture_screenshots_ok(tmp_path):
    session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    (tmp_path / "qa_screenshots").mkdir()

    results = await capture_screenshots(
        docker_session=session,
        feature_areas=["home", "login"],
        playwright_image="img",
        timeout_s=30,
        _run_fn=_ok_run,
    )

    assert len(results) == 2
    assert all(r.status == "ok" for r in results)
    assert results[0].feature_area == "home"
    assert results[1].feature_area == "login"


@pytest.mark.asyncio
async def test_capture_screenshots_partial_failure(tmp_path):
    call_count = [0]

    async def mock_run(cmd: list[str], timeout: float = 30.0):
        call_count[0] += 1
        if call_count[0] == 1:
            return 0, "", ""  # home ok
        return 1, "", "playwright error"  # login fails

    session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    (tmp_path / "qa_screenshots").mkdir()

    results = await capture_screenshots(
        docker_session=session,
        feature_areas=["home", "login"],
        playwright_image="img",
        timeout_s=30,
        _run_fn=mock_run,
    )

    assert results[0].status == "ok"
    assert results[1].status == "capture_failed"
    assert "playwright error" in (results[1].error or "")


# ---------------------------------------------------------------------------
# qa_screenshots node tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_no_workspace_returns_unchanged():
    state = _make_state(workspace_path=None)
    result = await qa_screenshots(state)
    assert result is state


@pytest.mark.asyncio
async def test_node_disabled_by_config(tmp_path):
    state = _make_state(workspace_path=tmp_path, qa_docker_enabled=False)
    result = await qa_screenshots(state)
    assert result is state  # state unchanged; qa_screenshots not updated by this node


@pytest.mark.asyncio
async def test_node_docker_unavailable_sets_empty_list(tmp_path):
    state = _make_state(workspace_path=tmp_path)

    with patch(
        "coordinare.graph.nodes.qa_screenshots.launch_docker_env",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await qa_screenshots(state)

    assert result["qa_screenshots"] == []


@pytest.mark.asyncio
async def test_node_captures_and_tears_down(tmp_path):
    state = _make_state(workspace_path=tmp_path)
    mock_session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    fake_results = [
        QAScreenshotResult(
            feature_area="home",
            file_path=tmp_path / "qa_screenshots" / "home.png",
            cdn_url=None,
            status="ok",
        )
    ]

    with (
        patch(
            "coordinare.graph.nodes.qa_screenshots.launch_docker_env",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.capture_screenshots",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_capture,
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ) as mock_teardown,
    ):
        result = await qa_screenshots(state)

    mock_capture.assert_awaited_once()
    mock_teardown.assert_awaited_once()
    assert len(result["qa_screenshots"]) == 1
    assert result["qa_screenshots"][0].status == "ok"


@pytest.mark.asyncio
async def test_node_teardown_called_even_on_capture_failure(tmp_path):
    """teardown_docker_env must run even if capture_screenshots raises."""
    state = _make_state(workspace_path=tmp_path)
    mock_session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path,
    )

    with (
        patch(
            "coordinare.graph.nodes.qa_screenshots.launch_docker_env",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.capture_screenshots",
            new_callable=AsyncMock,
            side_effect=RuntimeError("playwright crash"),
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ) as mock_teardown,pytest.raises(RuntimeError, match="playwright crash")
    ):
        await qa_screenshots(state)

    mock_teardown.assert_awaited_once()
