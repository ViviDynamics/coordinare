"""Unit tests for rebase data models (047)."""
from __future__ import annotations

from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound


def test_rebase_round_summary_with_performer_resolved() -> None:
    """Test summary includes performer_resolved conflicts."""
    round_obj = RebaseRound(trigger_pr_number=42, trigger_sha="abc123")
    round_obj.jobs = [
        RebaseJob(
            card_id="c1",
            branch="feat/x",
            outcome=RebaseOutcome.PERFORMER_RESOLVED,
            conflicted_files=["a.py", "b.py"],
        ),
    ]

    summary = round_obj.summary

    assert "performer_resolved" in summary or "conflict(s) resolved by performer" in summary
    assert "1 conflict(s) resolved by performer" in summary


def test_rebase_round_summary_with_all_outcomes() -> None:
    """Test summary includes all outcome types."""
    round_obj = RebaseRound(trigger_pr_number=42, trigger_sha="abc123")
    round_obj.jobs = [
        RebaseJob(card_id="c1", branch="clean", outcome=RebaseOutcome.CLEAN),
        RebaseJob(
            card_id="c2",
            branch="resolved",
            outcome=RebaseOutcome.PERFORMER_RESOLVED,
        ),
        RebaseJob(card_id="c3", branch="blocked", outcome=RebaseOutcome.BLOCKED),
        RebaseJob(card_id="c4", branch="skipped", outcome=RebaseOutcome.SKIPPED),
        RebaseJob(card_id="c5", branch="failed", outcome=RebaseOutcome.FAILED),
    ]

    summary = round_obj.summary

    assert "1 rebased cleanly" in summary
    assert "1 conflict(s) resolved by performer" in summary
    assert "1 blocked on conflict" in summary
    assert "1 skipped" in summary
    assert "1 failed" in summary


def test_rebase_round_summary_empty() -> None:
    """Test summary when no jobs exist."""
    round_obj = RebaseRound(trigger_pr_number=42, trigger_sha="abc123")
    round_obj.jobs = []

    summary = round_obj.summary

    assert summary == "no branches to rebase"


def test_rebase_job_to_dict() -> None:
    """Test RebaseJob to_dict serialization."""
    job = RebaseJob(
        card_id="c1",
        branch="feat/new",
        pr_number=42,
        pre_rebase_sha="sha1",
        post_rebase_sha="sha2",
        target_main_sha="main_sha",
        outcome=RebaseOutcome.CLEAN,
        conflicted_files=["a.py"],
        conflict_preview="conflict preview",
        duration_seconds=1.5,
    )

    result = job.to_dict()

    assert result["card_id"] == "c1"
    assert result["branch"] == "feat/new"
    assert result["pr_number"] == 42
    assert result["outcome"] == "clean"
    # Ephemeral fields (repo_dir, tmp_dir) should NOT be serialized
    assert "repo_dir" not in result
    assert "tmp_dir" not in result


def test_rebase_round_to_dict() -> None:
    """Test RebaseRound to_dict serialization."""
    round_obj = RebaseRound(trigger_pr_number=42, trigger_sha="abc123")
    round_obj.jobs = [
        RebaseJob(card_id="c1", branch="feat/x", outcome=RebaseOutcome.CLEAN),
    ]

    result = round_obj.to_dict()

    assert result["trigger_pr_number"] == 42
    assert result["trigger_sha"] == "abc123"
    assert "timestamp" in result
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["card_id"] == "c1"
