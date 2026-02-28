"""Unit tests for ClaudeScorer and compute_consensus (007, T011)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.advocate import IssueType, ScoringProvider
from coordinare.services.scoring import ClaudeScorer, ScoringResult, compute_consensus


def _make_claude_service(response_text: str | None = None, raise_exc: Exception | None = None) -> MagicMock:
    service = MagicMock()
    service._model = "claude-3-5-sonnet-latest"
    messages_mock = MagicMock()

    if raise_exc is not None:
        messages_mock.create = AsyncMock(side_effect=raise_exc)
    elif response_text is not None:
        msg_block = MagicMock()
        msg_block.text = response_text
        response = MagicMock()
        response.content = [msg_block]
        messages_mock.create = AsyncMock(return_value=response)
    else:
        response = MagicMock()
        response.content = []
        messages_mock.create = AsyncMock(return_value=response)

    service._client = MagicMock()
    service._client.messages = messages_mock
    return service


@pytest.mark.asyncio
async def test_claude_scorer_valid_json_response() -> None:
    """Valid JSON response → correct classification + confidence returned."""
    json_response = '{"classification": "question", "confidence": 0.85, "reasoning": "clear question", "response_text": "Based on `README.md`: the answer is X", "source_documents": ["README.md"]}'
    scorer = ClaudeScorer(_make_claude_service(response_text=json_response))

    result = await scorer.score("How does X work?", "I want to understand X.", "# README.md\nX works like this.")

    assert result.classification == IssueType.question
    assert result.provider.score == pytest.approx(0.85)
    assert result.response_text is not None
    assert "README.md" in result.source_documents


@pytest.mark.asyncio
async def test_claude_scorer_json_parse_failure() -> None:
    """JSON parse failure → ScoringResult with confidence=0.0 and reasoning='parse_error'."""
    scorer = ClaudeScorer(_make_claude_service(response_text="not valid json at all"))

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == 0.0
    assert result.provider.reasoning == "parse_error"
    assert result.classification is None
    assert result.response_text is None


@pytest.mark.asyncio
async def test_claude_scorer_api_exception() -> None:
    """API exception → ScoringResult with confidence=0.0 and reasoning='api_error'."""
    scorer = ClaudeScorer(_make_claude_service(raise_exc=RuntimeError("network error")))

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == 0.0
    assert result.provider.reasoning == "api_error"
    assert result.classification is None


@pytest.mark.asyncio
async def test_claude_scorer_clamps_confidence_out_of_range() -> None:
    """Confidence value outside [0,1] is clamped to [0,1]."""
    json_response = '{"classification": "question", "confidence": 1.5, "reasoning": "very sure", "response_text": null, "source_documents": []}'
    scorer = ClaudeScorer(_make_claude_service(response_text=json_response))

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_compute_consensus_mean_of_providers() -> None:
    """compute_consensus returns mean of successful (non-failed) provider scores."""
    provider_a = MagicMock()
    provider_a.score = AsyncMock(
        return_value=ScoringResult(
            provider=ScoringProvider(provider_name="a", score=0.8, reasoning=""),
            classification=IssueType.question,
            response_text="answer",
        )
    )
    provider_b = MagicMock()
    provider_b.score = AsyncMock(
        return_value=ScoringResult(
            provider=ScoringProvider(provider_name="b", score=0.6, reasoning=""),
            classification=IssueType.question,
            response_text=None,
        )
    )

    consensus, primary = await compute_consensus([provider_a, provider_b], "t", "b", "docs")

    assert consensus.final_score == pytest.approx(0.7)
    assert len(consensus.provider_scores) == 2
    assert primary.classification == IssueType.question


@pytest.mark.asyncio
async def test_compute_consensus_all_failed() -> None:
    """All providers fail → final_score=0.0."""
    provider = MagicMock()
    provider.score = AsyncMock(side_effect=RuntimeError("failed"))

    consensus, primary = await compute_consensus([provider], "t", "b", "docs")

    assert consensus.final_score == 0.0
    assert primary.classification is None
    assert primary.provider.provider_name == "none"
