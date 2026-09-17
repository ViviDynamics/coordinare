"""129 (US1): unit tests for the pure blocked-card recovery evaluator."""
from __future__ import annotations

from coordinare.services.blocked_recovery import (
    BlockReason,
    RecoverySignals,
    evaluate_recovery,
)


def test_no_reasons_stays_blocked() -> None:
    d = evaluate_recovery([], RecoverySignals())
    assert d.recover is False and "no recoverable" in d.reason


def test_stale_review_addressed_recovers_to_in_review() -> None:
    d = evaluate_recovery(
        [BlockReason.STALE_REVIEW],
        RecoverySignals(review_decision="CHANGES_REQUESTED", review_stale_addressed=True),
    )
    assert d.recover is True and d.target_stage == "IN_REVIEW"


def test_genuine_changes_requested_never_recovers() -> None:
    # FR-004: a fresh/genuine human verdict must NEVER auto-clear.
    d = evaluate_recovery(
        [BlockReason.STALE_REVIEW],
        RecoverySignals(review_decision="CHANGES_REQUESTED", review_stale_addressed=False),
    )
    assert d.recover is False


def test_verdict_cleared_recovers() -> None:
    d = evaluate_recovery(
        [BlockReason.STALE_REVIEW], RecoverySignals(review_decision="APPROVED"),
    )
    assert d.recover is True and d.target_stage == "IN_REVIEW"


def test_env_recovered_returns_to_prior_stage() -> None:
    d = evaluate_recovery(
        [BlockReason.ENV_BLOCKED],
        RecoverySignals(env_recovered=True, prior_stage="IN_PROGRESS"),
    )
    assert d.recover is True and d.target_stage == "IN_PROGRESS"


def test_env_still_broken_stays_blocked() -> None:
    d = evaluate_recovery([BlockReason.ENV_BLOCKED], RecoverySignals(env_recovered=False))
    assert d.recover is False


def test_ci_green_recovers() -> None:
    d = evaluate_recovery([BlockReason.CI_RED], RecoverySignals(ci_green=True))
    assert d.recover is True and d.target_stage == "IN_REVIEW"


def test_open_clarification_only_clears_when_answered() -> None:
    assert evaluate_recovery(
        [BlockReason.OPEN_CLARIFICATION], RecoverySignals(clarification_answered=False),
    ).recover is False
    d = evaluate_recovery(
        [BlockReason.OPEN_CLARIFICATION],
        RecoverySignals(clarification_answered=True, prior_stage="IN_PROGRESS"),
    )
    assert d.recover is True and d.target_stage == "IN_PROGRESS"


def test_multi_reason_all_must_clear() -> None:
    # env cleared but review still gated → stays blocked (FR-005)
    d = evaluate_recovery(
        [BlockReason.ENV_BLOCKED, BlockReason.STALE_REVIEW],
        RecoverySignals(env_recovered=True, review_decision="CHANGES_REQUESTED", review_stale_addressed=False),
    )
    assert d.recover is False
    # both cleared → recover to the latest stage (IN_REVIEW > IN_PROGRESS)
    d2 = evaluate_recovery(
        [BlockReason.ENV_BLOCKED, BlockReason.STALE_REVIEW],
        RecoverySignals(env_recovered=True, prior_stage="IN_PROGRESS",
                        review_decision="CHANGES_REQUESTED", review_stale_addressed=True),
    )
    assert d2.recover is True and d2.target_stage == "IN_REVIEW"


def test_cooldown_blocks_recovery() -> None:
    # FR-006: even when the block cleared, an active cooldown defers recovery.
    d = evaluate_recovery(
        [BlockReason.ENV_BLOCKED],
        RecoverySignals(env_recovered=True, cooldown_active=True),
    )
    assert d.recover is False and "cooldown" in d.reason
