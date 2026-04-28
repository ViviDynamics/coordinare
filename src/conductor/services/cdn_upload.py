"""GitHub CDN screenshot upload helper (055).

GitHub's unofficial asset upload flow:
  1. POST /repos/{org}/{repo}/issues/{issue_number}/asset-upload-url
     → {upload_url, asset_url, header}
  2. PUT upload_url with the file bytes (S3 presigned URL)
  3. Return asset_url as the embeddable CDN link

This is an undocumented API. We wrap it in a versioned helper and degrade
gracefully on any error (returns None, caller falls back to a placeholder).
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path  # noqa: TC003 — needed at runtime
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_DEFAULTmax_retries = 3
_RETRY_BASE_S = 1.0


async def upload_screenshot(
    file_path: Path,
    github_token: str,
    org: str,
    repo: str,
    issue_number: int,
    *,
    max_retries: int = _DEFAULTmax_retries,
    _client: Any = None,  # injection point for tests
) -> str | None:
    """Upload a screenshot PNG to GitHub CDN and return the embeddable URL.

    Returns None on any error (network, auth, rate-limit, unexpected API shape).
    Retries up to ``max_retries`` times with exponential backoff.
    """
    if not file_path.exists():
        logger.warning("cdn_upload.file_not_found", path=str(file_path))
        return None

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    policy_url = f"https://api.github.com/repos/{org}/{repo}/issues/{issue_number}/asset-upload-url"

    for attempt in range(1, max_retries + 1):
        try:
            if _client is None:
                ctx: Any = httpx.AsyncClient(timeout=30.0)
            else:
                ctx = contextlib.nullcontext(_client)
            async with ctx as c:
                # Step 1: request presigned upload URL
                resp = await c.post(
                    policy_url,
                    headers=headers,
                    json={"name": file_path.name, "size": file_path.stat().st_size},
                )
                if resp.status_code == 404:
                    logger.warning("cdn_upload.endpoint_not_found", url=policy_url)
                    return None
                if resp.status_code == 403:
                    logger.warning("cdn_upload.forbidden", status=resp.status_code)
                    return None
                resp.raise_for_status()

                payload = resp.json()
                upload_url: str | None = payload.get("upload_url")
                asset_url: str | None = payload.get("asset_url")
                upload_header: dict = payload.get("header") or {}

                if not upload_url or not asset_url:
                    logger.warning("cdn_upload.unexpected_response", keys=list(payload.keys()))
                    return None

                # Step 2: upload file bytes to S3 presigned URL
                file_bytes = file_path.read_bytes()
                put_resp = await c.put(
                    upload_url,
                    content=file_bytes,
                    headers={
                        "Content-Type": "image/png",
                        **upload_header,
                    },
                )
                put_resp.raise_for_status()

                logger.info(
                    "cdn_upload.ok",
                    file=file_path.name,
                    cdn_url=asset_url,
                    attempt=attempt,
                )
                return asset_url

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 503):
                wait = _RETRY_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "cdn_upload.retry",
                    attempt=attempt,
                    status=exc.response.status_code,
                    wait_s=wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.warning("cdn_upload.http_error", status=exc.response.status_code, error=str(exc))
                return None
        except Exception as exc:
            logger.warning("cdn_upload.error", attempt=attempt, error=str(exc))
            if attempt < max_retries:
                await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            else:
                return None

    return None


def _shot_field(shot: Any, key: str, default: Any = None) -> Any:
    if isinstance(shot, dict):
        return shot.get(key, default)
    return getattr(shot, key, default)


def render_screenshot_section(screenshots: list[Any]) -> str:
    """Render a markdown screenshot section for a QA comment.

    Accepts either ``QAScreenshotResult`` dataclasses or plain dicts with
    keys ``feature_area`` / ``cdn_url`` / ``status``. Groups by feature_area
    so repeated areas share a single heading. Returns an empty string when
    the list is empty; replaces failed entries with placeholders.
    """
    if not screenshots:
        return ""

    grouped: dict[str, list[Any]] = {}
    order: list[str] = []
    for shot in screenshots:
        area = _shot_field(shot, "feature_area", "unknown")
        if area not in grouped:
            grouped[area] = []
            order.append(area)
        grouped[area].append(shot)

    lines: list[str] = ["## Screenshots", ""]
    for area in order:
        lines.append(f"### {area}")
        for shot in grouped[area]:
            cdn_url = _shot_field(shot, "cdn_url")
            status = _shot_field(shot, "status", "skipped")
            if cdn_url:
                lines.append(f"![{area} screenshot]({cdn_url})")
            elif status == "capture_failed":
                lines.append("*(screenshot capture failed)*")
            else:
                lines.append("*(screenshot unavailable)*")
        lines.append("")

    return "\n".join(lines)
