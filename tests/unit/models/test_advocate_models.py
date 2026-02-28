"""Unit tests for advocate data models (007, T031)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.models.advocate import (
    ConsensusScore,
    EscalationReason,
    IssueClassification,
    IssueType,
    ScoringProvider,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TestIssueClassification:
    def test_valid_classification(self) -> None:
        c = IssueClassification(
            issue_id="issue-1",
            issue_number=42,
            classification=IssueType.question,
            confidence_score=0.85,
            sensitive_flagged=False,
            classified_at=_utc_now(),
        )
        assert c.confidence_score == 0.85

    def test_confidence_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence_score"):
            IssueClassification(
                issue_id="issue-1",
                issue_number=1,
                classification=IssueType.question,
                confidence_score=-0.1,
                sensitive_flagged=False,
                classified_at=_utc_now(),
            )

    def test_confidence_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence_score"):
            IssueClassification(
                issue_id="issue-1",
                issue_number=1,
                classification=IssueType.question,
                confidence_score=1.1,
                sensitive_flagged=False,
                classified_at=_utc_now(),
            )

    def test_empty_issue_id_raises(self) -> None:
        with pytest.raises(ValueError, match="issue_id"):
            IssueClassification(
                issue_id="",
                issue_number=1,
                classification=IssueType.question,
                confidence_score=0.5,
                sensitive_flagged=False,
                classified_at=_utc_now(),
            )

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            IssueClassification(
                issue_id="issue-1",
                issue_number=1,
                classification=IssueType.question,
                confidence_score=0.5,
                sensitive_flagged=False,
                classified_at=datetime(2026, 1, 1),  # naive
            )


class TestScoringProvider:
    def test_valid_provider(self) -> None:
        p = ScoringProvider(provider_name="claude", score=0.75, reasoning="high confidence")
        assert p.score == 0.75

    def test_score_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            ScoringProvider(provider_name="claude", score=-0.1, reasoning="x")

    def test_score_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            ScoringProvider(provider_name="claude", score=1.01, reasoning="x")

    def test_empty_provider_name_raises(self) -> None:
        with pytest.raises(ValueError, match="provider_name"):
            ScoringProvider(provider_name="", score=0.5, reasoning="x")


class TestConsensusScore:
    def test_final_score_is_mean(self) -> None:
        providers = [
            ScoringProvider(provider_name="a", score=0.8, reasoning=""),
            ScoringProvider(provider_name="b", score=0.6, reasoning=""),
        ]
        consensus = ConsensusScore(final_score=0.7, provider_scores=providers)
        assert consensus.final_score == pytest.approx(0.7)

    def test_issue_type_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            IssueType("invalid_type")

    def test_escalation_reason_values(self) -> None:
        assert EscalationReason.low_confidence.value == "low_confidence"
        assert EscalationReason.sensitive_keyword.value == "sensitive_keyword"
