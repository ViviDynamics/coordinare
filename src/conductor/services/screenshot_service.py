"""QA screenshot capture service (055).

Launches a Docker-based Playwright environment, waits for the app to be
reachable, captures screenshots per feature area, and returns results.

All Docker/Playwright paths are optional — any failure falls back to
a ``docker_unavailable`` or ``capture_failed`` result and QA continues.

Note: the inline Playwright script is Python, so the default image must be
a Playwright-for-Python image (``mcr.microsoft.com/playwright/python:...``),
not the JS image. Override via config ``qa_playwright_image`` if needed.

Note: ``--network=host`` only works on Linux. On macOS/Windows the docker
run will succeed but cannot reach a host-bound app server; production runs
are expected to be on Linux CI.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — needed at runtime by dataclass __init__
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

_DOCKER_BIN = "docker"
_DEFAULT_APP_URL = "http://localhost:3000"
_DEFAULT_TIMEOUT_S = 120
_STARTUP_POLL_INTERVAL_S = 2.0


@dataclass
class DockerSession:
    container_id: str
    app_url: str
    screenshot_dir: Path


@dataclass
class QAScreenshotResult:
    feature_area: str
    file_path: Path
    cdn_url: str | None
    status: Literal["ok", "upload_failed", "capture_failed", "skipped"]
    error: str | None = None


async def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(), stderr.decode()
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "subprocess timed out"


async def _docker_available(_run_fn: Any = None) -> bool:
    runner = _run_fn or _run
    rc, _, _ = await runner([_DOCKER_BIN, "version"], timeout=5.0)
    return rc == 0


async def launch_docker_env(
    playwright_image: str,
    workspace_path: Path,
    app_url: str = _DEFAULT_APP_URL,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    *,
    _run_fn: Any = None,  # injection point for tests
) -> DockerSession | None:
    """Pull the Playwright image, run docker-compose, and wait for the app.

    Returns a DockerSession on success, None if Docker is unavailable or the
    environment fails to start within timeout_s.
    """
    runner = _run_fn or _run

    if not shutil.which(_DOCKER_BIN):
        logger.warning("screenshot_service.docker_not_found")
        return None

    if not await _docker_available(_run_fn=runner):
        logger.warning("screenshot_service.docker_unavailable")
        return None

    screenshot_dir = workspace_path / "qa_screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Start docker-compose if a compose file is present; otherwise just pull
    compose_file = workspace_path / "docker-compose.yml"
    if not compose_file.exists():
        compose_file = workspace_path / "docker-compose.yaml"

    if compose_file.exists():
        rc, stdout, stderr = await runner(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", "--wait"],
            timeout=float(timeout_s),
        )
        if rc != 0:
            logger.warning(
                "screenshot_service.compose_up_failed",
                stderr=stderr[:300],
            )
            return None
        container_id = stdout.strip().splitlines()[-1] if stdout.strip() else "compose"
    else:
        rc, stdout, stderr = await runner(
            ["docker", "pull", playwright_image],
            timeout=60.0,
        )
        if rc != 0:
            logger.warning("screenshot_service.pull_failed", stderr=stderr[:300])
            return None
        container_id = playwright_image

    # Wait until app URL is reachable
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        rc, _, _ = await runner(
            ["docker", "run", "--rm", "--network=host", playwright_image,
             "curl", "-sf", "--max-time", "2", app_url],
            timeout=10.0,
        )
        if rc == 0:
            break
        await asyncio.sleep(_STARTUP_POLL_INTERVAL_S)
    else:
        logger.warning("screenshot_service.app_unreachable", app_url=app_url)
        return None

    logger.info("screenshot_service.env_ready", container_id=container_id, app_url=app_url)
    return DockerSession(
        container_id=container_id,
        app_url=app_url,
        screenshot_dir=screenshot_dir,
    )


async def capture_screenshots(
    docker_session: DockerSession,
    feature_areas: list[str],
    playwright_image: str,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    *,
    _run_fn: Any = None,
) -> list[QAScreenshotResult]:
    """Capture one screenshot per feature area using Playwright in Docker.

    Falls back to status='capture_failed' for any area that errors.
    """
    runner = _run_fn or _run
    results: list[QAScreenshotResult] = []

    # Inline Playwright script; URL and output filename are passed via env to
    # avoid quote/backslash injection if a feature_area or app_url contains
    # single quotes or backslashes.
    script = (
        "import os; "
        "from playwright.sync_api import sync_playwright; "
        "url = os.environ['APP_URL']; "
        "out = os.environ['OUT_PATH']; "
        "p = sync_playwright().start(); "
        "b = p.chromium.launch(); "
        "page = b.new_page(); "
        "page.goto(url); "
        "page.screenshot(path=out); "
        "b.close(); p.stop()"
    )

    for area in feature_areas:
        out_path = docker_session.screenshot_dir / f"{area}.png"
        rc, _, stderr = await runner(
            [
                "docker", "run", "--rm",
                "--network=host",
                "-v", f"{docker_session.screenshot_dir}:/screenshots",
                "-e", f"APP_URL={docker_session.app_url}",
                "-e", f"OUT_PATH=/screenshots/{area}.png",
                playwright_image,
                "python", "-c", script,
            ],
            timeout=float(timeout_s),
        )
        if rc != 0:
            logger.warning(
                "screenshot_service.capture_failed",
                area=area,
                stderr=stderr[:200],
            )
            results.append(QAScreenshotResult(
                feature_area=area,
                file_path=out_path,
                cdn_url=None,
                status="capture_failed",
                error=stderr[:200],
            ))
        else:
            logger.info("screenshot_service.captured", area=area, path=str(out_path))
            results.append(QAScreenshotResult(
                feature_area=area,
                file_path=out_path,
                cdn_url=None,
                status="ok",
            ))

    return results


async def teardown_docker_env(
    docker_session: DockerSession,
    workspace_path: Path,
    *,
    _run_fn: Any = None,
) -> None:
    """Stop and remove docker-compose services if they were started."""
    runner = _run_fn or _run
    compose_file = workspace_path / "docker-compose.yml"
    if not compose_file.exists():
        compose_file = workspace_path / "docker-compose.yaml"

    if compose_file.exists() and not docker_session.container_id.startswith("sha"):
        await runner(
            ["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"],
            timeout=30.0,
        )
        logger.info("screenshot_service.env_torn_down")
