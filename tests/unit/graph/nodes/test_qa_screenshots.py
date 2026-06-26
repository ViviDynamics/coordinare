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
async def test_node_docker_unavailable_records_not_captured(tmp_path):
    # 120 (US3/FR-015): docker unavailable must NOT read as an empty success —
    # it records an honest skipped result with a reason.
    state = _make_state(workspace_path=tmp_path)

    with patch(
        "coordinare.graph.nodes.qa_screenshots.launch_docker_env",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await qa_screenshots(state)

    shots = result["qa_screenshots"]
    assert len(shots) == 1
    assert shots[0].status == "skipped"
    assert "docker_unavailable" in (shots[0].error or "")


@pytest.mark.asyncio
async def test_node_skips_capture_when_app_did_not_boot(tmp_path):
    # 120 (US3/FR-014): no boot-proof → skip launch, record honest not-captured.
    state = _make_state(workspace_path=tmp_path)
    state["qa_app_boot_ok"] = False

    launch = AsyncMock(return_value=None)
    with patch(
        "coordinare.graph.nodes.qa_screenshots.launch_docker_env", launch
    ):
        result = await qa_screenshots(state)

    launch.assert_not_called()  # did not even attempt capture
    shots = result["qa_screenshots"]
    assert len(shots) == 1
    assert shots[0].status == "skipped"
    assert "app_boot_unverified" in (shots[0].error or "")


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


# ---------------------------------------------------------------------------
# CDN upload paths (lines 84-117)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_token_fetch_exception_skips_upload(tmp_path):
    """If github.current_token() raises, upload is skipped but screenshots are still returned."""
    state = _make_state(workspace_path=tmp_path)

    class _GitHubBadToken:
        org = "org"
        project_name = "repo"

        async def current_token(self):
            raise RuntimeError("vault unavailable")

    state["github_service"] = _GitHubBadToken()
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

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
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ),
    ):
        result = await qa_screenshots(state)

    # Screenshots captured even though token fetch failed (upload skipped)
    assert len(result["qa_screenshots"]) == 1
    assert result["qa_screenshots"][0].cdn_url is None


@pytest.mark.asyncio
async def test_node_cdn_upload_called_when_all_fields_present(tmp_path):
    """CDN upload is triggered for 'ok' screenshots when token, org, repo and issue are set."""
    state = _make_state(workspace_path=tmp_path)

    class _GitHubGoodToken:
        org = "myorg"
        project_name = "myrepo"

        async def current_token(self):
            return "ghp_goodtoken"

    state["github_service"] = _GitHubGoodToken()
    state["current_card"] = {"id": "ITEM_1", "issue_number": 7}

    shot_path = tmp_path / "qa_screenshots" / "home.png"
    shot_path.parent.mkdir(parents=True, exist_ok=True)
    shot_path.write_bytes(b"\x89PNG\r\n")

    mock_session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    fake_results = [
        QAScreenshotResult(
            feature_area="home",
            file_path=shot_path,
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
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.upload_screenshot",
            new_callable=AsyncMock,
            return_value="https://cdn.example.com/home.png",
        ) as mock_upload,
    ):
        result = await qa_screenshots(state)

    mock_upload.assert_awaited_once()
    assert result["qa_screenshots"][0].cdn_url == "https://cdn.example.com/home.png"


@pytest.mark.asyncio
async def test_node_cdn_upload_failure_marks_upload_failed(tmp_path):
    """When CDN upload returns None, the screenshot status is set to 'upload_failed'."""
    state = _make_state(workspace_path=tmp_path)

    class _GitHubGoodToken:
        org = "myorg"
        project_name = "myrepo"

        async def current_token(self):
            return "ghp_goodtoken"

    state["github_service"] = _GitHubGoodToken()
    state["current_card"] = {"id": "ITEM_1", "issue_number": 7}

    shot_path = tmp_path / "qa_screenshots" / "home.png"
    shot_path.parent.mkdir(parents=True, exist_ok=True)
    shot_path.write_bytes(b"\x89PNG\r\n")

    mock_session = DockerSession(
        container_id="abc",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    fake_results = [
        QAScreenshotResult(
            feature_area="home",
            file_path=shot_path,
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
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.upload_screenshot",
            new_callable=AsyncMock,
            return_value=None,  # upload fails
        ),
    ):
        result = await qa_screenshots(state)

    assert result["qa_screenshots"][0].status == "upload_failed"
