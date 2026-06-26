"""QA screenshot capture LangGraph node (055).

Runs after the QA checks complete (monitoring_performer phase). If the active
card has a workspace path and QA Docker is enabled, launches the Docker
Playwright environment, captures screenshots per feature area, and writes
results to state["qa_screenshots"].

Gracefully skips if:
- Docker is disabled in config
- No workspace path is set
- Docker environment fails to start (returns [] screenshots, logs a warning)
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from coordinare.services.cdn_upload import upload_screenshot
from coordinare.services.screenshot_service import (
    QAScreenshotResult,
    capture_screenshots,
    launch_docker_env,
    teardown_docker_env,
)

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_DEFAULT_FEATURE_AREAS = ["home", "main_feature"]
_DEFAULT_PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python:v1.44.0-jammy"


def _not_captured(reason: str) -> QAScreenshotResult:
    """120 (US3/FR-015): an honest 'no screenshot captured' record.

    Returned instead of a silent empty list so a screenshot-less backstop run
    can never read as one that captured evidence. ``status='skipped'`` with a
    names-only reason in ``error``."""
    return QAScreenshotResult(
        feature_area="(none)",
        file_path=Path(""),
        cdn_url=None,
        status="skipped",
        error=f"no screenshot captured: {reason}",
    )


async def qa_screenshots(state: CoordinareState) -> CoordinareState:
    config = state.get("config")
    workspace_path = state.get("workspace_path")

    if not workspace_path:
        return state

    # Respect config flag — default to enabled if config is absent
    if config is not None and not getattr(config, "qa_docker_enabled", True):
        logger.debug("qa_screenshots.disabled_by_config")
        return state

    card = state.get("current_card") or {}
    card_id = str(card.get("id", ""))

    # 120 (US3/FR-014): only attempt capture when the app actually booted in QA.
    # The QA run records whether app_boot_check passed (state["qa_app_boot_ok"]).
    # If it explicitly did NOT boot, record an honest skip rather than launching
    # a capture that can only fail against a dead app. Unknown (None) → proceed;
    # launch_docker_env itself waits on reachability.
    if state.get("qa_app_boot_ok") is False:
        logger.info("qa_screenshots.skipped_no_boot_proof", card_id=card_id)
        state["qa_screenshots"] = [_not_captured("app_boot_unverified")]
        return state

    playwright_image = (
        getattr(config, "qa_playwright_image", None) or _DEFAULT_PLAYWRIGHT_IMAGE
        if config is not None
        else _DEFAULT_PLAYWRIGHT_IMAGE
    )
    timeout_s: int = (
        getattr(config, "qa_screenshot_timeout_s", 120)
        if config is not None
        else 120
    )

    docker_session = await launch_docker_env(
        playwright_image=playwright_image,
        workspace_path=workspace_path,
        timeout_s=timeout_s,
    )

    if docker_session is None:
        # 120 (US3/FR-015): never report an empty success — record the skip.
        logger.warning("qa_screenshots.docker_unavailable", card_id=card_id)
        state["qa_screenshots"] = [_not_captured("docker_unavailable")]
        return state

    try:
        results: list[QAScreenshotResult] = await capture_screenshots(
            docker_session=docker_session,
            feature_areas=_DEFAULT_FEATURE_AREAS,
            playwright_image=playwright_image,
            timeout_s=timeout_s,
        )
    finally:
        await teardown_docker_env(docker_session, workspace_path)

    # Phase 4: attempt CDN upload for each captured screenshot
    github = state.get("github_service")
    github_token: str = ""
    github_org: str = ""
    github_repo: str = ""
    if github is not None:
        try:
            github_token = await github.current_token()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("qa_screenshots.token_unavailable", error=str(exc))
        github_org = getattr(github, "org", "") or ""
        github_repo = getattr(github, "project_name", "") or ""
    issue_number: int = int(card.get("issue_number") or 0)

    upload_retries: int = (
        getattr(config, "qa_screenshot_upload_retries", 3)
        if config is not None
        else 3
    )

    if github_token and github_org and github_repo and issue_number:
        for result in results:
            if result.status == "ok":
                cdn_url = await upload_screenshot(
                    file_path=result.file_path,
                    github_token=github_token,
                    org=github_org,
                    repo=github_repo,
                    issue_number=issue_number,
                    max_retries=upload_retries,
                )
                if cdn_url:
                    result.cdn_url = cdn_url
                else:
                    result.status = "upload_failed"

    logger.info(
        "qa_screenshots.complete",
        card_id=card_id,
        total=len(results),
        ok=sum(1 for r in results if r.status == "ok"),
        upload_failed=sum(1 for r in results if r.status == "upload_failed"),
        capture_failed=sum(1 for r in results if r.status == "capture_failed"),
    )

    state["qa_screenshots"] = results
    return state
