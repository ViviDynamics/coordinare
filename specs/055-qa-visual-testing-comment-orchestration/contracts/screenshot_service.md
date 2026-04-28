# Contract: ScreenshotService

**Module**: `src/coordinare/services/screenshot_service.py` (orchestration) + `agent/performer/qa/screenshots.py` (capture)

## Interface

```python
class DockerSession:
    container_id: str
    base_url: str               # e.g. "http://localhost:3000"
    playwright_page: Page       # Playwright Page object

async def launch_docker_env(config: CoordinareConfig) -> DockerSession | None:
    """Pull and launch the Playwright Docker image; boot the app inside it.

    Returns None if Docker is unavailable or qa_docker_enabled=False.
    Raises DockerTimeoutError if app does not respond within qa_screenshot_timeout_s.
    """

async def capture_screenshots(
    docker_session: DockerSession,
    feature_areas: list[str],
    workspace_path: Path,
    timeout_s: int,
) -> list[QAScreenshotResult]:
    """Navigate to each feature area URL; capture and save a PNG.

    feature_areas: list of (label, path) pairs, e.g. [("login", "/login"), ("dashboard", "/")]
    Returns one QAScreenshotResult per area.
    """

async def teardown_docker_env(docker_session: DockerSession) -> None:
    """Stop and remove the Docker container."""
```

## Behavior Guarantees

- `launch_docker_env` MUST return `None` (not raise) if Docker is not installed or `qa_docker_enabled=False`.
- `capture_screenshots` MUST respect `timeout_s` per screenshot; individual timeouts MUST NOT cancel remaining screenshots.
- All temporary screenshot files MUST be written to `{workspace_path}/qa_screenshots/`.
- `teardown_docker_env` MUST be called in a `finally` block regardless of capture outcome.

## Error Classification

| Condition | Result |
|-----------|--------|
| Docker not installed | `launch_docker_env` returns `None` |
| Image pull fails | `launch_docker_env` returns `None`; logs warning |
| App doesn't start in time | `launch_docker_env` raises `DockerTimeoutError` → caller catches, returns `None` |
| Screenshot capture times out | `QAScreenshotResult(status="capture_failed", error="timeout")` |
| Playwright not installed | `launch_docker_env` returns `None`; logs warning |
