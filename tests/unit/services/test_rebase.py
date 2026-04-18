"""Unit tests for the auto-rebase service (047)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from coordinare.models.rebase import RebaseJob, RebaseOutcome
from coordinare.services.rebase import (
    build_conflict_block_comment,
    build_conflict_feedback,
    detect_stale_branches,
    fetch_main_sha,
    force_push_with_lease,
    prepare_conflict_resolution,
    rebase_branch,
)

# ---------------------------------------------------------------------------
# T010: fetch_main_sha tests
# ---------------------------------------------------------------------------


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
        import tempfile

        from coordinare.services.rebase import _run_git

        with tempfile.TemporaryDirectory() as tmp, \
             patch("asyncio.wait_for", side_effect=TimeoutError):
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
# T012: rebase_branch tests
# ---------------------------------------------------------------------------


class TestRebaseBranch:
    @pytest.mark.asyncio
    async def test_clean_rebase(self) -> None:
        """Clean rebase returns CLEAN with post_rebase_sha."""
        call_count = {"n": 0}

        async def mock_git(args, cwd, *, env=None, timeout=120):
            call_count["n"] += 1
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""  # different from main_sha → not up-to-date
            if cmd == "rebase":
                return 0, "", ""
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )

        assert job.outcome == RebaseOutcome.CLEAN
        assert job.post_rebase_sha == "deadbeef"

    @pytest.mark.asyncio
    async def test_conflict_detected(self) -> None:
        """Conflict returns BLOCKED with conflicted files."""
        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 1, "", "CONFLICT (content): Merge conflict in src/app.py"
            if cmd == "diff":
                return 0, "src/app.py", ""
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("coordinare.services.rebase.extract_conflict_info",
                   new_callable=AsyncMock,
                   return_value=(["src/app.py"], "<<<< HEAD\nours\n====\ntheirs\n>>>>")), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )

        assert job.outcome == RebaseOutcome.BLOCKED
        assert "src/app.py" in job.conflicted_files

    @pytest.mark.asyncio
    async def test_already_up_to_date(self) -> None:
        """Branch whose merge-base == main_sha returns SKIPPED."""
        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "newmainsha", ""  # same as main_sha → up to date
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )

        assert job.outcome == RebaseOutcome.SKIPPED

    @pytest.mark.asyncio
    async def test_fetch_branch_failure(self) -> None:
        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 128, "", "fatal: couldn't find remote ref"
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )
        assert job.outcome == RebaseOutcome.FAILED
        assert "Fetch branch failed" in job.conflict_preview

    @pytest.mark.asyncio
    async def test_checkout_failure(self) -> None:
        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 128, "", "error: pathspec 'branch' did not match"
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )
        assert job.outcome == RebaseOutcome.FAILED
        assert "Checkout failed" in job.conflict_preview

    @pytest.mark.asyncio
    async def test_force_push_failure_after_clean_rebase(self) -> None:
        """Clean rebase but force-push fails → FAILED outcome."""
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_pr",
                "current_card": {"pr_url": "https://github.com/o/r/pull/42"},
            },
        }

        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd in ("clone", "fetch", "checkout"):
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 0, "", ""
            if cmd == "push":
                return 128, "", "rejected"
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            rr = await run_rebase_round(
                sessions, "newmainsha", "https://github.com/o/r.git", "tok",
            )

        assert len(rr.jobs) == 1
        assert rr.jobs[0].outcome == RebaseOutcome.FAILED

    @pytest.mark.asyncio
    async def test_clone_failure(self) -> None:
        async def mock_git(args, cwd, *, env=None, timeout=120):
            return 128, "", "fatal: repository not found"

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )

        assert job.outcome == RebaseOutcome.FAILED
        assert "Clone failed" in job.conflict_preview


# ---------------------------------------------------------------------------
# T013: force_push_with_lease tests
# ---------------------------------------------------------------------------


class TestForcePushWithLease:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "", "")
            result = await force_push_with_lease(
                "/tmp/repo", "coordinare/PVTI_X/feat", "oldsha", "tok",
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_lease_rejection_fails_cleanly(self) -> None:
        """Lease rejection fails (no retry — next rebase round re-clones
        and re-rebases from the updated remote state)."""
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (128, "", "stale info")
            result = await force_push_with_lease(
                "/tmp/repo", "coordinare/PVTI_X/feat", "oldsha", "tok",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_persistent_failure(self) -> None:
        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (128, "", "rejected")
            result = await force_push_with_lease(
                "/tmp/repo", "coordinare/PVTI_X/feat", "oldsha", "tok",
            )
        assert result is False


# ---------------------------------------------------------------------------
# T024: build_conflict_feedback tests
# ---------------------------------------------------------------------------


class TestBuildConflictFeedback:
    def test_includes_file_list_and_preview(self) -> None:
        job = RebaseJob(
            card_id="CARD_A",
            branch="coordinare/PVTI_A/feat",
            target_main_sha="abc123",
            conflicted_files=["src/app.py", "src/utils.py"],
            conflict_preview="<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>",
        )
        feedback = build_conflict_feedback(job)
        assert len(feedback) == 2
        assert "src/app.py" in feedback[0]["body"]
        assert "src/utils.py" in feedback[0]["body"]
        assert "<<<<<<< HEAD" in feedback[1]["body"]

    def test_empty_when_no_conflicts(self) -> None:
        job = RebaseJob(card_id="CARD_A", branch="b")
        assert build_conflict_feedback(job) == []


# ---------------------------------------------------------------------------
# T025: prepare_conflict_resolution tests
# ---------------------------------------------------------------------------


class TestPrepareConflictResolution:
    def test_sets_state_for_implementer_dispatch(self) -> None:
        job = RebaseJob(
            card_id="CARD_A",
            branch="coordinare/PVTI_A/feat",
            target_main_sha="abc123",
            conflicted_files=["src/app.py"],
            conflict_preview="conflict markers here",
        )
        state: dict = {"relay_feedback": [], "performer_stage": "reviewing", "phase": "idle"}
        prepare_conflict_resolution(job, state)

        assert state["performer_stage"] == "implementing"
        assert state["phase"] == "dispatching"
        assert len(state["relay_feedback"]) == 2
        assert "src/app.py" in state["relay_feedback"][0]["body"]


# ---------------------------------------------------------------------------
# T026: build_conflict_block_comment tests
# ---------------------------------------------------------------------------


class TestBuildConflictBlockComment:
    def test_includes_files_preview_and_mentions(self) -> None:
        job = RebaseJob(
            card_id="CARD_A",
            branch="coordinare/PVTI_A/feat",
            target_main_sha="abc123def456",
            conflicted_files=["src/app.py"],
            conflict_preview="<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>",
        )
        comment = build_conflict_block_comment(job, human_reviewers=["alice", "bob"])

        assert "@alice" in comment
        assert "@bob" in comment
        assert "src/app.py" in comment
        assert "abc123de" in comment  # truncated SHA
        assert "<<<<<<< HEAD" in comment

    def test_no_mentions_when_no_reviewers(self) -> None:
        job = RebaseJob(
            card_id="CARD_A", branch="b", target_main_sha="abc",
            conflicted_files=["f.py"], conflict_preview="x",
        )
        comment = build_conflict_block_comment(job)
        assert not comment.startswith("@")


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
# T031: run_rebase_round integration test
# ---------------------------------------------------------------------------


class TestRunRebaseRound:
    @pytest.mark.asyncio
    async def test_round_with_clean_rebase(self) -> None:
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_pr",
                "current_card": {"pr_url": "https://github.com/o/r/pull/42"},
            },
        }

        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 0, "", ""
            if cmd == "push":
                return 0, "", ""
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            rr = await run_rebase_round(
                sessions, "newmainsha", "https://github.com/o/r.git", "tok",
            )

        assert len(rr.jobs) == 1
        assert rr.jobs[0].outcome == RebaseOutcome.CLEAN
        assert "1 rebased cleanly" in rr.summary

    @pytest.mark.asyncio
    async def test_round_skips_active_performer(self) -> None:
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_performer",
                "current_card": {"pr_url": "https://github.com/o/r/pull/42"},
            },
        }
        rr = await run_rebase_round(
            sessions, "newmainsha", "https://github.com/o/r.git", "tok",
        )
        # Active-performer branches appear in jobs as SKIPPED so the
        # Slack summary / dashboard show them (not silently omitted).
        assert len(rr.jobs) == 1
        assert rr.jobs[0].outcome == RebaseOutcome.SKIPPED

    @pytest.mark.asyncio
    async def test_round_blocked_job_defers_comment_to_caller(self) -> None:
        """When a job is BLOCKED, run_rebase_round does NOT post a comment
        (the caller handles it after attempting performer resolution)."""
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_pr",
                "current_card": {
                    "pr_url": "https://github.com/o/r/pull/42",
                    "issue_id": "I_kwDO_A",
                },
            },
        }

        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 1, "", "CONFLICT"
            return 0, "", ""

        github = AsyncMock()
        github.add_comment = AsyncMock()

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("coordinare.services.rebase.extract_conflict_info",
                   new_callable=AsyncMock,
                   return_value=(["src/app.py"], "<<<< HEAD\nours")), \
             patch("shutil.rmtree"):
            rr = await run_rebase_round(
                sessions, "newmainsha", "https://github.com/o/r.git", "tok",
                github=github, human_reviewers=["alice"],
            )

        assert len(rr.jobs) == 1
        assert rr.jobs[0].outcome == RebaseOutcome.BLOCKED
        # Comment NOT posted in run_rebase_round — caller handles it
        github.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_round_dispatches_slack_notification(self) -> None:
        """run_rebase_round dispatches a Slack notification for non-skipped
        outcomes."""
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_pr",
                "current_card": {"pr_url": "https://github.com/o/r/pull/42"},
            },
        }

        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd in ("clone", "fetch", "checkout", "push"):
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 0, "", ""
            return 0, "", ""

        notification_svc = AsyncMock()
        notification_svc.dispatch = AsyncMock()

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            rr = await run_rebase_round(
                sessions, "newmainsha", "https://github.com/o/r.git", "tok",
                notification_service=notification_svc,
            )

        assert len(rr.jobs) == 1
        notification_svc.dispatch.assert_called_once()
        event = notification_svc.dispatch.call_args[0][0]
        assert "rebase_round_complete" in event.payload["event_type"]

    @pytest.mark.asyncio
    async def test_round_no_notification_when_all_skipped(self) -> None:
        """No Slack notification when all branches are skipped."""
        from coordinare.services.rebase import run_rebase_round

        sessions = {
            "CARD_A": {
                "workspace_branch": "coordinare/PVTI_A/feat-a",
                "phase": "monitoring_pr",
                "current_card": {"pr_url": "https://github.com/o/r/pull/42"},
            },
        }

        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd in ("clone", "fetch", "checkout"):
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "newmainsha", ""  # up to date → SKIPPED
            return 0, "", ""

        notification_svc = AsyncMock()

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("shutil.rmtree"):
            await run_rebase_round(
                sessions, "newmainsha", "https://github.com/o/r.git", "tok",
                notification_service=notification_svc,
            )

        notification_svc.dispatch.assert_not_called()


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


class TestExtractConflictInfo:
    @pytest.mark.asyncio
    async def test_extracts_conflict_markers_from_files(self) -> None:
        """Exercise the file-reading path in extract_conflict_info."""
        import os
        import tempfile

        from coordinare.services.rebase import extract_conflict_info

        with tempfile.TemporaryDirectory() as tmp:
            # Create a fake conflicted file
            conflict_content = (
                "line 1\n"
                "<<<<<<< HEAD\n"
                "our version\n"
                "=======\n"
                "their version\n"
                ">>>>>>> main\n"
                "line 2\n"
            )
            os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
            with open(os.path.join(tmp, "src/app.py"), "w") as f:
                f.write(conflict_content)

            with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
                mock.return_value = (0, "src/app.py", "")
                files, preview = await extract_conflict_info(tmp)

        assert files == ["src/app.py"]
        assert "<<<<<<< HEAD" in preview
        assert "our version" in preview
        assert "their version" in preview

    @pytest.mark.asyncio
    async def test_handles_unreadable_file(self) -> None:
        """When a conflicted file can't be read, preview says so."""
        from coordinare.services.rebase import extract_conflict_info

        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "nonexistent/file.py", "")
            files, preview = await extract_conflict_info("/tmp/fakerepo")

        assert files == ["nonexistent/file.py"]
        assert "could not read" in preview

    @pytest.mark.asyncio
    async def test_no_conflicted_files(self) -> None:
        from coordinare.services.rebase import extract_conflict_info

        with patch("coordinare.services.rebase._run_git", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "", "")
            files, preview = await extract_conflict_info("/tmp/fakerepo")

        assert files == []
        assert preview == ""


class TestRebaseBranchNonConflictFailure:
    @pytest.mark.asyncio
    async def test_non_conflict_rebase_failure_classified_as_failed(self) -> None:
        """When rebase exits non-zero but extract_conflict_info finds no
        conflicted files, outcome should be FAILED (not BLOCKED)."""
        async def mock_git(args, cwd, *, env=None, timeout=120):
            cmd = args[0] if args else ""
            if cmd == "clone":
                return 0, "", ""
            if cmd == "fetch":
                return 0, "", ""
            if cmd == "checkout":
                return 0, "", ""
            if cmd == "rev-parse":
                return 0, "deadbeef", ""
            if cmd == "merge-base":
                return 0, "oldsha", ""
            if cmd == "rebase":
                return 1, "", "fatal: invalid upstream"
            return 0, "", ""

        with patch("coordinare.services.rebase._run_git", side_effect=mock_git), \
             patch("coordinare.services.rebase.extract_conflict_info",
                   new_callable=AsyncMock, return_value=([], "")), \
             patch("shutil.rmtree"):
            job = await rebase_branch(
                "https://github.com/o/r.git", "coordinare/PVTI_X/feat",
                "newmainsha", "tok",
            )

        assert job.outcome == RebaseOutcome.FAILED
        assert "not a merge conflict" in job.conflict_preview
