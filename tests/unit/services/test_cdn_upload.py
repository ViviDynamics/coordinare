"""Tests for CDN upload helper (055 Phase 4)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.services.cdn_upload import render_screenshot_section, upload_screenshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png(tmp_path: Path, name: str = "shot.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n")
    return p


def _mock_client(policy_status: int = 200, policy_body: dict | None = None, put_status: int = 200):
    """Return an AsyncMock httpx client with configurable responses."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    policy_resp = MagicMock()
    policy_resp.status_code = policy_status
    policy_resp.json.return_value = policy_body or {
        "upload_url": "https://s3.example.com/presigned",
        "asset_url": "https://github.com/user-attachments/shot.png",
        "header": {},
    }
    policy_resp.raise_for_status = MagicMock()

    put_resp = MagicMock()
    put_resp.status_code = put_status
    put_resp.raise_for_status = MagicMock()

    client.post = AsyncMock(return_value=policy_resp)
    client.put = AsyncMock(return_value=put_resp)
    return client


# ---------------------------------------------------------------------------
# upload_screenshot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_ok(tmp_path):
    png = _make_png(tmp_path)
    client = _mock_client()
    url = await upload_screenshot(
        file_path=png,
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
        _client=client,
    )
    assert url == "https://github.com/user-attachments/shot.png"


@pytest.mark.asyncio
async def test_upload_file_not_found(tmp_path):
    url = await upload_screenshot(
        file_path=tmp_path / "missing.png",
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_endpoint_404(tmp_path):
    png = _make_png(tmp_path)
    client = _mock_client(policy_status=404)
    client.post.return_value.raise_for_status = MagicMock()
    url = await upload_screenshot(
        file_path=png,
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
        _client=client,
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_unexpected_response_shape(tmp_path):
    png = _make_png(tmp_path)
    client = _mock_client(policy_body={"something": "unexpected"})
    url = await upload_screenshot(
        file_path=png,
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
        _client=client,
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_forbidden(tmp_path):
    png = _make_png(tmp_path)
    client = _mock_client(policy_status=403)
    client.post.return_value.raise_for_status = MagicMock()
    url = await upload_screenshot(
        file_path=png,
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
        _client=client,
    )
    assert url is None


# ---------------------------------------------------------------------------
# render_screenshot_section tests
# ---------------------------------------------------------------------------


def test_render_empty_returns_empty():
    assert render_screenshot_section([]) == ""


def test_render_ok_screenshot():
    shots = [{"feature_area": "login", "cdn_url": "https://cdn.example.com/img.png", "status": "ok"}]
    md = render_screenshot_section(shots)
    assert "## Screenshots" in md
    assert "![login screenshot](https://cdn.example.com/img.png)" in md


def test_render_upload_failed_placeholder():
    shots = [{"feature_area": "dashboard", "cdn_url": None, "status": "upload_failed"}]
    md = render_screenshot_section(shots)
    assert "*(screenshot unavailable)*" in md


def test_render_capture_failed_placeholder():
    shots = [{"feature_area": "settings", "cdn_url": None, "status": "capture_failed"}]
    md = render_screenshot_section(shots)
    assert "*(screenshot capture failed)*" in md


def test_render_mixed_screenshots():
    shots = [
        {"feature_area": "home", "cdn_url": "https://cdn.example.com/home.png", "status": "ok"},
        {"feature_area": "login", "cdn_url": None, "status": "upload_failed"},
    ]
    md = render_screenshot_section(shots)
    assert "![home screenshot]" in md
    assert "*(screenshot unavailable)*" in md
    assert "### home" in md
    assert "### login" in md


# ---------------------------------------------------------------------------
# upload_screenshot retry/error paths (lines 57, 104-122)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_429_retries_then_succeeds(tmp_path) -> None:
    """429 response triggers exponential backoff retry; second attempt succeeds."""
    from unittest.mock import patch as _patch

    import httpx as _httpx

    png = _make_png(tmp_path)

    r429 = _httpx.Response(429, request=_httpx.Request("POST", "https://api.github.com/x"))
    exc_429 = _httpx.HTTPStatusError("429 Too Many Requests", request=r429.request, response=r429)

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "upload_url": "https://s3.example.com/presigned",
        "asset_url": "https://github.com/user-attachments/shot.png",
        "header": {},
    }
    success_resp.raise_for_status = MagicMock()

    fail_resp = MagicMock()
    fail_resp.status_code = 429
    fail_resp.raise_for_status = MagicMock(side_effect=exc_429)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=[fail_resp, success_resp])
    client.put = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    with _patch("asyncio.sleep", new_callable=AsyncMock):
        url = await upload_screenshot(
            file_path=png,
            github_token="tok",
            org="Org",
            repo="repo",
            issue_number=1,
            _client=client,
            max_retries=3,
        )

    assert url == "https://github.com/user-attachments/shot.png"
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_upload_non_retriable_http_error_returns_none(tmp_path) -> None:
    """Non-429/503 HTTPStatusError returns None without retry."""
    import httpx as _httpx

    png = _make_png(tmp_path)

    r500 = _httpx.Response(500, request=_httpx.Request("POST", "https://api.github.com/x"))
    exc_500 = _httpx.HTTPStatusError("500 Server Error", request=r500.request, response=r500)

    fail_resp = MagicMock()
    fail_resp.status_code = 500
    fail_resp.raise_for_status = MagicMock(side_effect=exc_500)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=fail_resp)

    url = await upload_screenshot(
        file_path=png,
        github_token="tok",
        org="Org",
        repo="repo",
        issue_number=1,
        _client=client,
        max_retries=3,
    )

    assert url is None
    assert client.post.await_count == 1  # no retry for non-retriable errors


@pytest.mark.asyncio
async def test_upload_generic_exception_retries_then_returns_none(tmp_path) -> None:
    """Generic exception retries up to max_retries then returns None."""
    from unittest.mock import patch as _patch

    png = _make_png(tmp_path)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=RuntimeError("unexpected error"))

    with _patch("asyncio.sleep", new_callable=AsyncMock):
        url = await upload_screenshot(
            file_path=png,
            github_token="tok",
            org="Org",
            repo="repo",
            issue_number=1,
            _client=client,
            max_retries=2,
        )

    assert url is None
    assert client.post.await_count == 2  # retried max_retries times


# ---------------------------------------------------------------------------
# render_screenshot_section with object-type shots (lines 130, 148-151)
# ---------------------------------------------------------------------------


def test_render_object_type_shots() -> None:
    """render_screenshot_section handles QAScreenshotResult objects (not dicts)."""
    from types import SimpleNamespace

    shots = [
        SimpleNamespace(feature_area="dashboard", cdn_url="https://cdn.example.com/dash.png", status="ok"),
        SimpleNamespace(feature_area="settings", cdn_url=None, status="upload_failed"),
        SimpleNamespace(feature_area="profile", cdn_url=None, status="capture_failed"),
    ]
    md = render_screenshot_section(shots)

    assert "## Screenshots" in md
    assert "### dashboard" in md
    assert "![dashboard screenshot](https://cdn.example.com/dash.png)" in md
    assert "### settings" in md
    assert "*(screenshot unavailable)*" in md
    assert "### profile" in md
    assert "*(screenshot capture failed)*" in md
