"""Tests for performer.cli (agent-callable CLI shims)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from performer.cli import fetch_ci_log_cli, upload_screenshot_cli
from performer.github import GitHubAPIError


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


# --------------------------------------------------------------------------
# performer-fetch-ci-log
# --------------------------------------------------------------------------

@pytest.fixture
def ci_env(monkeypatch):
    monkeypatch.setenv("PERFORMER_GH_TOKEN", "ghp_test")
    monkeypatch.setenv("PERFORMER_GH_OWNER", "acme")
    monkeypatch.setenv("PERFORMER_GH_REPO", "widgets")
    monkeypatch.setenv("PERFORMER_GH_ISSUE", "42")


def _patch_failing(failing):
    async def fake(token, owner, repo, pr):
        return failing
    return patch("performer.cli._list_failing", side_effect=fake)


def test_fetch_ci_log_missing_env(monkeypatch, capsys):
    for v in ("PERFORMER_GH_TOKEN", "PERFORMER_GH_OWNER", "PERFORMER_GH_REPO", "PERFORMER_GH_ISSUE"):
        monkeypatch.delenv(v, raising=False)
    rc = fetch_ci_log_cli(["--list"])
    assert rc == 1
    assert "missing context" in capsys.readouterr().err


def test_fetch_ci_log_list_empty(ci_env, capsys):
    with _patch_failing([]):
        rc = fetch_ci_log_cli(["--list"])
    assert rc == 0
    assert "no failing checks" in capsys.readouterr().out


def test_fetch_ci_log_list_prints_failures(ci_env, capsys):
    failing = [
        {"id": 111, "name": "lint", "conclusion": "failure"},
        {"id": 222, "name": "test", "conclusion": "timed_out"},
    ]
    with _patch_failing(failing):
        rc = fetch_ci_log_cli(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "111\tfailure\tlint" in out
    assert "222\ttimed_out\ttest" in out


def test_fetch_ci_log_check_not_found(ci_env, capsys):
    failing = [{"id": 111, "name": "lint", "conclusion": "failure"}]
    with _patch_failing(failing):
        rc = fetch_ci_log_cli(["--check", "missing"])
    assert rc == 3
    assert "no failing check named" in capsys.readouterr().err


def test_fetch_ci_log_check_fetches_log(ci_env, capsys):
    failing = [{
        "id": 111,
        "name": "lint",
        "conclusion": "failure",
        "output": {"title": "Lint failed", "summary": "2 errors"},
    }]
    async def fake_logs(owner, repo, job_id, token, *, max_chars):
        assert job_id == 111
        return "ERROR in file.py:10"
    with _patch_failing(failing), patch("performer.cli.get_check_run_logs", side_effect=fake_logs):
        rc = fetch_ci_log_cli(["--check", "lint"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Lint failed" in out
    assert "2 errors" in out
    assert "ERROR in file.py:10" in out


def test_fetch_ci_log_api_error(ci_env, capsys):
    async def boom(token, owner, repo, pr):
        raise GitHubAPIError(403, "forbidden")
    with patch("performer.cli._list_failing", side_effect=boom):
        rc = fetch_ci_log_cli(["--list"])
    assert rc == 2
    assert "GitHub API error" in capsys.readouterr().err
