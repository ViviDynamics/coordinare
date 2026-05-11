"""Tests for CDN upload helper (qa-assets branch upload)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.services.cdn_upload import render_screenshot_section, upload_screenshot


def _make_png(tmp_path: Path, name: str = "shot.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n")
    return p


class _FakeGit:
    def __init__(self, *, clone_rc: int = 0, push_rcs: list[int] | None = None):
        self.clone_rc = clone_rc
        self.push_rcs = list(push_rcs or [])
        self.calls: list[list[str]] = []

    async def __call__(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        # Token must never appear in argv — it goes through GIT_ASKPASS.
        for a in argv:
            assert "SECRET-TOKEN" not in a, f"token leaked into argv: {a!r}"
        if argv[:2] == ["git", "clone"]:
            target = Path(argv[-1])
            if self.clone_rc == 0:
                target.mkdir(parents=True, exist_ok=True)
            return self.clone_rc, "", ""
        if argv[:2] == ["git", "init"]:
            cwd.mkdir(parents=True, exist_ok=True)
            return 0, "", ""
        if argv[:2] == ["git", "push"]:
            rc = self.push_rcs.pop(0) if self.push_rcs else 0
            return rc, "", "non-fast-forward" if rc != 0 else ""
        return 0, "", ""


# ---------------------------------------------------------------------------
# upload_screenshot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_ok(tmp_path):
    png = _make_png(tmp_path)
    git = _FakeGit()
    url = await upload_screenshot(
        file_path=png,
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=1,
        _runner=git,
    )
    assert url is not None
    assert url.startswith("https://github.com/Org/repo/raw/qa-assets/content/1/")
    assert url.endswith("-shot.png")


@pytest.mark.asyncio
async def test_upload_bootstraps_orphan_when_clone_fails(tmp_path):
    png = _make_png(tmp_path)
    git = _FakeGit(clone_rc=128)
    url = await upload_screenshot(
        file_path=png,
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=2,
        _runner=git,
    )
    assert url is not None
    cmds = [c[1] for c in git.calls if len(c) > 1]
    assert "init" in cmds
    assert "remote" in cmds


@pytest.mark.asyncio
async def test_upload_file_not_found(tmp_path):
    url = await upload_screenshot(
        file_path=tmp_path / "missing.png",
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=1,
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_missing_context(tmp_path):
    png = _make_png(tmp_path)
    assert await upload_screenshot(
        file_path=png, github_token="", org="o", repo="r", issue_number=1,
    ) is None
    assert await upload_screenshot(
        file_path=png, github_token="t", org="o", repo="r", issue_number=0,
    ) is None


@pytest.mark.asyncio
async def test_upload_retries_on_push_then_succeeds(tmp_path, monkeypatch):
    png = _make_png(tmp_path)
    git = _FakeGit(push_rcs=[1, 0])

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("coordinare.services.cdn_upload.asyncio.sleep", _no_sleep)

    url = await upload_screenshot(
        file_path=png,
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=3,
        _runner=git,
        max_retries=3,
    )
    assert url is not None
    pushes = [c for c in git.calls if c[:2] == ["git", "push"]]
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_upload_push_keeps_failing_returns_none(tmp_path, monkeypatch):
    png = _make_png(tmp_path)
    git = _FakeGit(push_rcs=[1, 1])

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("coordinare.services.cdn_upload.asyncio.sleep", _no_sleep)

    url = await upload_screenshot(
        file_path=png,
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=3,
        _runner=git,
        max_retries=2,
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_runner_exception_retries_then_returns_none(tmp_path, monkeypatch):
    png = _make_png(tmp_path)

    async def bad_runner(argv, cwd, env=None):
        raise RuntimeError("boom")

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("coordinare.services.cdn_upload.asyncio.sleep", _no_sleep)

    url = await upload_screenshot(
        file_path=png,
        github_token="SECRET-TOKEN",
        org="Org",
        repo="repo",
        issue_number=3,
        _runner=bad_runner,
        max_retries=2,
    )
    assert url is None


# ---------------------------------------------------------------------------
# render_screenshot_section tests (unchanged behavior)
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


def test_render_object_type_shots() -> None:
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
    assert "*(screenshot unavailable)*" in md
    assert "*(screenshot capture failed)*" in md
