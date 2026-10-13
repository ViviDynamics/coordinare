"""Unit tests for the branch-divergence service (047; git plumbing removed by 555)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from coordinare.services.rebase import (
    detect_stale_branches,
    fetch_main_sha,
    repo_url_from_config,
)

# ---------------------------------------------------------------------------
# _run_git direct test (covers actual subprocess path)
# ---------------------------------------------------------------------------


class TestRunGit:
    @pytest.mark.asyncio
    async def test_runs_git_command(self) -> None:
        """Exercise the actual _run_git subprocess path with git --version."""
        import tempfile

        from coordinare.services.rebase import _run_git

        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, _stderr = await _run_git(["--version"], cwd=tmp)
        assert exit_code == 0
        assert "git version" in stdout

    @pytest.mark.asyncio
    async def test_timeout_returns_128(self) -> None:
        """Mock asyncio.wait_for to raise TimeoutError so we exercise
        the _run_git timeout handling path without a real long-running
        process."""
        import inspect
        import tempfile

        from coordinare.services.rebase import _run_git

        async def _fake_wait_for(coro, *, timeout=None):
            if inspect.iscoroutine(coro):
                coro.close()
            raise TimeoutError()

        with tempfile.TemporaryDirectory() as tmp, \
             patch("asyncio.wait_for", new=_fake_wait_for):
            exit_code, _stdout, stderr = await _run_git(
                ["--version"], cwd=tmp, timeout=1,
            )
        assert exit_code == 128
        assert "timed out" in stderr


class TestFetchMainSha:
    @pytest.mark.asyncio
    async def test_returns_sha_on_success(self) -> None:
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "abc123def456\trefs/heads/main", "")
            result = await fetch_main_sha("https://github.com/o/r.git", "tok")
        assert result == "abc123def456"

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self) -> None:
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (128, "", "fatal: could not read")
            result = await fetch_main_sha("https://github.com/o/r.git", "tok")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_output(self) -> None:
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "", "")
            result = await fetch_main_sha("https://github.com/o/r.git", "tok")
        assert result is None


# ---------------------------------------------------------------------------
# T011: detect_stale_branches tests
# ---------------------------------------------------------------------------


def _session(
    branch: str = "coordinare/PVTI_X/feat",
    phase: str = "monitoring_pr",
    pr_url: str = "https://github.com/o/r/pull/42",
) -> dict:
    return {
        "workspace_branch": branch,
        "phase": phase,
        "current_card": {"pr_url": pr_url},
    }


class TestDetectStaleBranches:
    def test_no_sessions(self) -> None:
        assert detect_stale_branches({}, "abc123") == []

    def test_returns_stale_branches(self) -> None:
        sessions = {
            "CARD_A": _session(branch="coordinare/PVTI_A/feat-a"),
            "CARD_B": _session(branch="coordinare/PVTI_B/feat-b"),
        }
        result = detect_stale_branches(sessions, "main_sha")
        assert len(result) == 2
        assert result[0]["card_id"] == "CARD_A"
        assert result[1]["card_id"] == "CARD_B"

    def test_skips_active_performer(self) -> None:
        """FR-006: branches with monitoring_performer phase are included
        but marked skipped=True so the summary/dashboard shows them."""
        sessions = {
            "CARD_A": _session(phase="monitoring_performer"),
            "CARD_B": _session(phase="monitoring_pr"),
        }
        result = detect_stale_branches(sessions, "main_sha")
        assert len(result) == 2
        skipped = [r for r in result if r.get("skipped")]
        active = [r for r in result if not r.get("skipped")]
        assert len(skipped) == 1
        assert skipped[0]["card_id"] == "CARD_A"
        assert len(active) == 1
        assert active[0]["card_id"] == "CARD_B"

    def test_skips_non_coordinare_branches(self) -> None:
        sessions = {
            "CARD_A": _session(branch="feature/something"),
        }
        assert detect_stale_branches(sessions, "main_sha") == []

    def test_skips_sessions_without_pr(self) -> None:
        sessions = {
            "CARD_A": _session(pr_url=""),
        }
        assert detect_stale_branches(sessions, "main_sha") == []

    def test_extracts_pr_number(self) -> None:
        sessions = {
            "CARD_A": _session(pr_url="https://github.com/o/r/pull/99"),
        }
        result = detect_stale_branches(sessions, "main_sha")
        assert result[0]["pr_number"] == 99


# ---------------------------------------------------------------------------
# Model serialization + summary tests
# ---------------------------------------------------------------------------


class TestRebaseModels:
    def test_rebase_job_to_dict(self) -> None:
        from coordinare.models.rebase import RebaseJob, RebaseOutcome
        job = RebaseJob(
            card_id="CARD_A", branch="coordinare/PVTI_A/feat",
            pr_number=42, pre_rebase_sha="aaa", post_rebase_sha="bbb",
            target_main_sha="ccc", outcome=RebaseOutcome.CLEAN,
            duration_seconds=3.5,
        )
        d = job.to_dict()
        assert d["card_id"] == "CARD_A"
        assert d["outcome"] == "clean"
        assert d["duration_seconds"] == 3.5

    def test_rebase_round_to_dict(self) -> None:
        from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound
        rr = RebaseRound(trigger_pr_number=10, trigger_sha="abc")
        rr.jobs.append(RebaseJob(card_id="A", branch="b", outcome=RebaseOutcome.CLEAN))
        d = rr.to_dict()
        assert d["trigger_pr_number"] == 10
        assert len(d["jobs"]) == 1
        assert "timestamp" in d

    def test_rebase_round_summary(self) -> None:
        from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound
        rr = RebaseRound()
        rr.jobs = [
            RebaseJob(card_id="A", branch="b1", outcome=RebaseOutcome.CLEAN),
            RebaseJob(card_id="B", branch="b2", outcome=RebaseOutcome.BLOCKED),
            RebaseJob(card_id="C", branch="b3", outcome=RebaseOutcome.SKIPPED),
        ]
        s = rr.summary
        assert "1 rebased cleanly" in s
        assert "1 blocked on conflict" in s
        assert "1 skipped" in s

    def test_rebase_round_empty_summary(self) -> None:
        from coordinare.models.rebase import RebaseRound
        rr = RebaseRound()
        assert rr.summary == "no branches to rebase"


# ---------------------------------------------------------------------------
# Coverage: _run_git exception path, extract_conflict_info
# ---------------------------------------------------------------------------


class TestRunGitExceptionPath:
    @pytest.mark.asyncio
    async def test_general_exception_returns_128(self) -> None:
        """_run_git returns (128, '', error_msg) when create_subprocess_exec
        raises an unexpected exception."""
        from coordinare.services.rebase import _run_git

        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no git")):
            exit_code, _stdout, stderr = await _run_git(["--version"], cwd="/tmp")
        assert exit_code == 128
        assert "no git" in stderr


# ---------------------------------------------------------------------------
# repo_url_from_config tests
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal stand-in for ProjectConfiguration."""

    def __init__(self, org: str, project: str, api_url: str = "", git_base_url: str | None = None) -> None:
        self.github_org = org
        self.project_name = project
        self.github_api_url = api_url
        # Mirror pydantic: git_base_url always has the default value, but
        # model_fields_set records only what the operator actually wrote.
        self.git_base_url = "https://github.com" if git_base_url is None else git_base_url
        self.model_fields_set = frozenset() if git_base_url is None else frozenset({"git_base_url"})


class TestRepoUrlFromConfig:
    def test_public_github(self) -> None:
        cfg = _Cfg("myorg", "myrepo")
        assert repo_url_from_config(cfg) == "https://github.com/myorg/myrepo.git"

    def test_public_github_explicit_api_url(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://api.github.com")
        assert repo_url_from_config(cfg) == "https://github.com/myorg/myrepo.git"

    def test_ghes_host(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://github.example.com/api/v3")
        assert repo_url_from_config(cfg) == "https://github.example.com/myorg/myrepo.git"

    def test_ghes_non_standard_port(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://github.example.com:8443/api/v3")
        assert repo_url_from_config(cfg) == "https://github.example.com:8443/myorg/myrepo.git"

    def test_missing_org_returns_empty(self) -> None:
        cfg = _Cfg("", "myrepo")
        assert repo_url_from_config(cfg) == ""

    def test_missing_project_returns_empty(self) -> None:
        cfg = _Cfg("myorg", "")
        assert repo_url_from_config(cfg) == ""

    def test_userinfo_stripped_from_clone_url(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://user:token@github.example.com/api/v3")
        result = repo_url_from_config(cfg)
        assert result == "https://github.example.com/myorg/myrepo.git"
        assert "user" not in result
        assert "token" not in result

    def test_ipv6_host_bracketed(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://[::1]:8443/api/v3")
        result = repo_url_from_config(cfg)
        assert result == "https://[::1]:8443/myorg/myrepo.git"

    def test_ipv6_host_no_port(self) -> None:
        cfg = _Cfg("myorg", "myrepo", "https://[::1]/api/v3")
        result = repo_url_from_config(cfg)
        assert result == "https://[::1]/myorg/myrepo.git"

    def test_git_base_url_override_used_when_set(self) -> None:
        # 151: the bench sets git_base_url to a loopback git:// daemon; it must
        # win over the REST github_api_url (which would yield a bogus https URL).
        cfg = _Cfg("bench-org", "bench-repo", "http://127.0.0.1:35377", "git://127.0.0.1:9418")
        assert repo_url_from_config(cfg) == "git://127.0.0.1:9418/bench-org/bench-repo.git"

    def test_git_base_url_default_falls_through_to_api_derivation(self) -> None:
        # An UNSET git_base_url must NOT override GHES api-derived hosts.
        cfg = _Cfg("myorg", "myrepo", "https://github.example.com/api/v3")
        assert repo_url_from_config(cfg) == "https://github.example.com/myorg/myrepo.git"

    def test_git_base_url_explicitly_set_to_default_still_wins(self) -> None:
        # Explicitly writing the default host is a choice, not a fall-through:
        # model_fields_set separates the two, so it beats the GHES derivation.
        cfg = _Cfg("myorg", "myrepo", "https://github.example.com/api/v3", "https://github.com")
        assert repo_url_from_config(cfg) == "https://github.com/myorg/myrepo.git"
