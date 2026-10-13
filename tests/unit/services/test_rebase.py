"""Unit tests for the branch-divergence service (047; git plumbing removed by 555)."""
from __future__ import annotations

from coordinare.services.rebase import detect_stale_branches

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
