"""Unit tests for BackendScorer and compute_consensus (007, T011)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.advocate import IssueType, ScoringProvider
from coordinare.services.scoring import BackendScorer, ScoringResult, compute_consensus


def _make_backend(
    data: dict[str, Any] | None = None,
    text: str = "",
    raise_exc: Exception | None = None,
) -> MagicMock:
    backend = MagicMock()
    if raise_exc is not None:
        backend.prompt = AsyncMock(side_effect=raise_exc)
    else:
        backend.prompt = AsyncMock(return_value={"text": text, "data": data})
    return backend


@pytest.mark.asyncio
async def test_scorer_valid_json_response() -> None:
    data = {
        "classification": "question",
        "confidence": 0.85,
        "reasoning": "clear question",
        "response_text": "Based on `README.md`: the answer is X",
        "source_documents": ["README.md"],
    }
    scorer = BackendScorer(_make_backend(data=data), provider_name="test")

    result = await scorer.score("How does X work?", "I want to understand X.", "# README.md")

    assert result.classification == IssueType.question
    assert result.provider.score == pytest.approx(0.85)
    assert result.response_text is not None
    assert "README.md" in result.source_documents


@pytest.mark.asyncio
async def test_scorer_parse_failure_no_data() -> None:
    scorer = BackendScorer(_make_backend(data=None, text="not valid json"), provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == 0.0
    assert result.provider.reasoning == "parse_error"
    assert result.classification is None
    assert result.response_text is None


@pytest.mark.asyncio
async def test_scorer_retries_once_on_parse_error_then_succeeds() -> None:
    """Small open-source models sometimes drop the JSON envelope on first try."""
    good_data = {
        "classification": "question",
        "confidence": 0.7,
        "reasoning": "ok",
        "response_text": None,
        "source_documents": [],
    }
    backend = MagicMock()
    backend.prompt = AsyncMock(
        side_effect=[
            {"text": "garbage", "data": None},
            {"text": "good", "data": good_data},
        ]
    )
    scorer = BackendScorer(backend, provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert backend.prompt.await_count == 2
    assert result.classification == IssueType.question
    assert result.provider.score == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_scorer_api_exception() -> None:
    scorer = BackendScorer(_make_backend(raise_exc=RuntimeError("network error")), provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == 0.0
    assert result.provider.reasoning == "api_error"
    assert result.classification is None


@pytest.mark.asyncio
async def test_scorer_clamps_confidence_out_of_range() -> None:
    data = {
        "classification": "question",
        "confidence": 1.5,
        "reasoning": "very sure",
        "response_text": None,
        "source_documents": [],
    }
    scorer = BackendScorer(_make_backend(data=data), provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert result.provider.score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_compute_consensus_mean_of_providers() -> None:
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
    provider = MagicMock()
    provider.score = AsyncMock(side_effect=RuntimeError("failed"))

    consensus, primary = await compute_consensus([provider], "t", "b", "docs")

    assert consensus.final_score == 0.0
    assert primary.classification is None
    assert primary.provider.provider_name == "none"
