"""Unit tests for performer.cdn_upload (qa-assets branch upload)."""
from __future__ import annotations

from pathlib import Path

import pytest

from performer.cdn_upload import (
    is_image_path,
    is_local_path,
    resolve_visual_evidence_urls,
    upload_screenshot,
)


def test_is_local_path():
    assert is_local_path("/tmp/foo.png")
    assert is_local_path("screenshots/a.png")
    assert not is_local_path("https://example.com/x.png")
    assert not is_local_path("http://example.com/x.png")
    assert not is_local_path("")


def test_is_image_path():
    assert is_image_path("a.png")
    assert is_image_path("a.PNG")
    assert is_image_path("foo/bar.jpeg")
    assert not is_image_path("notes.txt")


class _FakeGit:
    """Fake git runner that records invocations and simulates outcomes.

    Behavior is controlled by ``clone_rc`` (return code for the initial
    clone) and ``push_rcs`` (list popped per push attempt — exhausted
    list defaults to 0).
    """

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


@pytest.mark.asyncio
async def test_upload_screenshot_success_via_clone(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"PNG")
    git = _FakeGit()

    url = await upload_screenshot(
        img, "SECRET-TOKEN", "Org", "repo", 42, _runner=git,
    )

    assert url is not None
    assert url.startswith("https://github.com/Org/repo/raw/qa-assets/content/42/")
    assert url.endswith("-shot.png")
    cmds = [c[1] for c in git.calls if len(c) > 1]
    assert "clone" in cmds
    assert "add" in cmds
    assert "commit" in cmds
    assert "push" in cmds


@pytest.mark.asyncio
async def test_upload_screenshot_bootstraps_orphan_when_clone_fails(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"PNG")
    git = _FakeGit(clone_rc=128)

    url = await upload_screenshot(
        img, "SECRET-TOKEN", "Org", "repo", 7, _runner=git,
    )

    assert url is not None
    cmds = [c[1] for c in git.calls if len(c) > 1]
    assert "init" in cmds
    assert "remote" in cmds
    # init invoked with -b qa-assets
    init = next(c for c in git.calls if c[:2] == ["git", "init"])
    assert init == ["git", "init", "-b", "qa-assets"]


@pytest.mark.asyncio
async def test_upload_screenshot_missing_file(tmp_path):
    url = await upload_screenshot(tmp_path / "nope.png", "SECRET-TOKEN", "o", "r", 1)
    assert url is None


@pytest.mark.asyncio
async def test_upload_screenshot_missing_context(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"x")
    assert await upload_screenshot(img, "", "o", "r", 1) is None
    assert await upload_screenshot(img, "SECRET-TOKEN", "", "r", 1) is None
    assert await upload_screenshot(img, "SECRET-TOKEN", "o", "", 1) is None
    assert await upload_screenshot(img, "SECRET-TOKEN", "o", "r", 0) is None


@pytest.mark.asyncio
async def test_upload_screenshot_retries_on_push_failure(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    git = _FakeGit(push_rcs=[1, 0])

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("performer.cdn_upload.asyncio.sleep", _no_sleep)

    url = await upload_screenshot(
        img, "SECRET-TOKEN", "o", "r", 5, _runner=git, max_retries=3,
    )
    assert url is not None
    pushes = [c for c in git.calls if c[:2] == ["git", "push"]]
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_upload_screenshot_returns_none_when_push_keeps_failing(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    git = _FakeGit(push_rcs=[1, 1])

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("performer.cdn_upload.asyncio.sleep", _no_sleep)

    url = await upload_screenshot(
        img, "SECRET-TOKEN", "o", "r", 5, _runner=git, max_retries=2,
    )
    assert url is None


@pytest.mark.asyncio
async def test_resolve_visual_evidence_urls_replaces_local_paths(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")

    async def fake_uploader(path, token, org, repo, issue_number):
        assert path == f
        return "https://github.com/o/r/raw/qa-assets/content/5/ts-shot.png"

    evidence = [
        {"label": "S1", "kind": "screenshot", "path_or_url": "shot.png", "note": ""},
        {"label": "Already", "kind": "screenshot", "path_or_url": "https://x/y.png", "note": ""},
    ]
    out = await resolve_visual_evidence_urls(
        evidence,
        workspace_root=tmp_path,
        github_token="SECRET-TOKEN",
        org="o",
        repo="r",
        issue_number=5,
        uploader=fake_uploader,
    )
    assert out[0]["path_or_url"].startswith("https://github.com/o/r/raw/qa-assets/")
    assert out[1]["path_or_url"] == "https://x/y.png"


@pytest.mark.asyncio
async def test_resolve_visual_evidence_urls_missing_prereqs_returns_unchanged():
    evidence = [{"label": "x", "kind": "screenshot", "path_or_url": "/tmp/x.png", "note": ""}]
    out = await resolve_visual_evidence_urls(
        evidence,
        workspace_root=Path("/tmp"),
        github_token="",
        org="o",
        repo="r",
        issue_number=0,
    )
    assert out == evidence


@pytest.mark.asyncio
async def test_resolve_visual_evidence_urls_upload_failure_keeps_path(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")

    async def failing_uploader(*a, **k):
        return None

    evidence = [{"label": "S1", "kind": "screenshot", "path_or_url": "shot.png", "note": ""}]
    out = await resolve_visual_evidence_urls(
        evidence,
        workspace_root=tmp_path,
        github_token="SECRET-TOKEN",
        org="o",
        repo="r",
        issue_number=5,
        uploader=failing_uploader,
    )
    assert out[0]["path_or_url"] == "shot.png"
