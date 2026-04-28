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
