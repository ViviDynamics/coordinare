"""Tests for performer.cli (agent-callable CLI shims)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from performer.cli import upload_screenshot_cli


@pytest.fixture
def good_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFORMER_GH_TOKEN", "ghp_test")
    monkeypatch.setenv("PERFORMER_GH_OWNER", "acme")
    monkeypatch.setenv("PERFORMER_GH_REPO", "widgets")
    monkeypatch.setenv("PERFORMER_GH_ISSUE", "42")


def test_cli_success_prints_url(good_env, capsys, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG")
    url = "https://github.com/user-attachments/assets/abc"

    async def _fake_upload(*args, **kwargs):
        return url

    with patch("performer.cli.upload_screenshot", side_effect=_fake_upload):
        rc = upload_screenshot_cli([str(img)])

    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == url


def test_cli_missing_context_exits_1(monkeypatch, capsys, tmp_path):
    for var in ("PERFORMER_GH_TOKEN", "PERFORMER_GH_OWNER", "PERFORMER_GH_REPO", "PERFORMER_GH_ISSUE"):
        monkeypatch.delenv(var, raising=False)
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")

    rc = upload_screenshot_cli([str(img)])
    out = capsys.readouterr()
    assert rc == 1
    assert "missing context" in out.err


def test_cli_upload_failure_exits_2(good_env, capsys, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")

    async def _fake_upload(*args, **kwargs):
        return None

    with patch("performer.cli.upload_screenshot", side_effect=_fake_upload):
        rc = upload_screenshot_cli([str(img)])

    out = capsys.readouterr()
    assert rc == 2
    assert "upload failed" in out.err


def test_cli_upload_exception_exits_2(good_env, capsys, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")

    async def _fake_upload(*args, **kwargs):
        raise RuntimeError("boom")

    with patch("performer.cli.upload_screenshot", side_effect=_fake_upload):
        rc = upload_screenshot_cli([str(img)])

    out = capsys.readouterr()
    assert rc == 2
    assert "upload error" in out.err
    assert "boom" in out.err
