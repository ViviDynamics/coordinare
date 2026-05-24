"""Coverage tests for scoring.py — empty-results consensus and parse/api error paths."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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


# ---------------------------------------------------------------------------
# compute_consensus — all providers fail → fallback ScoringResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_consensus_empty_providers_returns_fallback() -> None:
    from coordinare.models.advocate import ConsensusScore

    consensus, primary = await compute_consensus([], "title", "body", "docs")

    assert isinstance(consensus, ConsensusScore)
    assert consensus.final_score == 0.0
    assert consensus.provider_scores == []
    assert primary.provider.provider_name == "none"
    assert primary.classification is None


@pytest.mark.asyncio
async def test_compute_consensus_all_providers_fail_returns_fallback() -> None:
    class FailingProvider:
        @property
        def provider_name(self) -> str:
            return "failing"

        async def score(self, issue_title: str, issue_body: str, doc_content: str) -> ScoringResult:
            raise RuntimeError("provider down")

    consensus, primary = await compute_consensus(
        [FailingProvider(), FailingProvider()],
        "title",
        "body",
        "docs",
    )

    assert consensus.final_score == 0.0
    assert primary.provider.provider_name == "none"
    assert primary.provider.reasoning == "all_failed"
    assert primary.classification is None


@pytest.mark.asyncio
async def test_compute_consensus_all_api_errors_returns_fallback() -> None:
    """BackendScorer api_error results have classification=None → treated as failed."""
    scorer = BackendScorer(_make_backend(raise_exc=RuntimeError("timeout")), provider_name="test")

    consensus, primary = await compute_consensus([scorer], "title", "body", "docs")

    assert consensus.final_score == 0.0
    assert primary.provider.provider_name == "none"


# ---------------------------------------------------------------------------
# BackendScorer.score() — parse/api error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scorer_no_data_returns_parse_error() -> None:
    """response['data'] is None → parse_error."""
    scorer = BackendScorer(_make_backend(data=None), provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.response_text is None
    assert result.provider.provider_name == "test"
    assert result.provider.reasoning == "parse_error"


@pytest.mark.asyncio
async def test_scorer_missing_classification_returns_parse_error() -> None:
    """data missing required 'classification' key → parse_error."""
    scorer = BackendScorer(
        _make_backend(data={"confidence": 0.5}), provider_name="test"
    )

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.provider.reasoning == "parse_error"


@pytest.mark.asyncio
async def test_scorer_invalid_classification_value_returns_parse_error() -> None:
    """Unknown classification value → parse_error."""
    scorer = BackendScorer(
        _make_backend(data={"classification": "not_a_real_type", "confidence": 0.5}),
        provider_name="test",
    )

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.provider.reasoning == "parse_error"


def test_scorer_provider_name() -> None:
    scorer = BackendScorer(MagicMock(), provider_name="anthropic_api")
    assert scorer.provider_name == "anthropic_api"


@pytest.mark.asyncio
async def test_scorer_backend_exception_returns_api_error() -> None:
    scorer = BackendScorer(_make_backend(raise_exc=RuntimeError("boom")), provider_name="test")

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.provider.reasoning == "api_error"
