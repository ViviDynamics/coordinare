"""Coverage tests for scoring.py — empty-results consensus and no-content ClaudeScorer path."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.services.scoring import ClaudeScorer, ScoringResult, compute_consensus


def _make_claude_service(
    response_text: str | None = None,
    raise_exc: Exception | None = None,
    empty_content: bool = False,
) -> MagicMock:
    """Build a mock ClaudeService.

    - raise_exc: messages.create raises this exception
    - empty_content: response.content is [] (no content blocks)
    - response_text: response has one content block with this text
    """
    service = MagicMock()
    service._model = "claude-3-5-sonnet-latest"
    messages_mock = MagicMock()

    if raise_exc is not None:
        messages_mock.create = AsyncMock(side_effect=raise_exc)
    elif empty_content:
        response = MagicMock()
        response.content = []
        messages_mock.create = AsyncMock(return_value=response)
    elif response_text is not None:
        msg_block = MagicMock()
        msg_block.text = response_text
        response = MagicMock()
        response.content = [msg_block]
        messages_mock.create = AsyncMock(return_value=response)
    else:
        # content attribute is None / falsy
        response = MagicMock()
        response.content = None
        messages_mock.create = AsyncMock(return_value=response)

    service._client = MagicMock()
    service._client.messages = messages_mock
    return service


# ---------------------------------------------------------------------------
# compute_consensus — all providers fail → fallback ScoringResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_consensus_empty_providers_returns_fallback() -> None:
    """With no providers at all, consensus score is 0.0 and primary is 'none'."""
    from coordinare.models.advocate import ConsensusScore

    consensus, primary = await compute_consensus([], "title", "body", "docs")

    assert isinstance(consensus, ConsensusScore)
    assert consensus.final_score == 0.0
    assert consensus.provider_scores == []
    assert primary.provider.provider_name == "none"
    assert primary.classification is None


@pytest.mark.asyncio
async def test_compute_consensus_all_providers_fail_returns_fallback() -> None:
    """When every provider raises, valid results are empty and fallback is used."""
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
    """ClaudeScorer api_error results have classification=None → treated as failed."""
    scorer = ClaudeScorer(_make_claude_service(raise_exc=RuntimeError("timeout")))

    consensus, primary = await compute_consensus(
        [scorer],
        "title",
        "body",
        "docs",
    )

    assert consensus.final_score == 0.0
    assert primary.provider.provider_name == "none"


# ---------------------------------------------------------------------------
# ClaudeScorer.score() — no content in response → returns _failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_scorer_empty_content_list_returns_failure() -> None:
    """response.content == [] → no text extracted → _failure returned."""
    scorer = ClaudeScorer(_make_claude_service(empty_content=True))

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.response_text is None
    assert result.provider.provider_name == "claude"
    assert result.provider.reasoning == "api_error"


@pytest.mark.asyncio
async def test_claude_scorer_none_content_returns_failure() -> None:
    """response.content is None (falsy) → _failure returned."""
    scorer = ClaudeScorer(_make_claude_service())  # empty_content=False, no text → content=None

    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.provider.reasoning == "api_error"


def test_claude_scorer_provider_name() -> None:
    """ClaudeScorer.provider_name property returns 'claude'."""
    scorer = ClaudeScorer(MagicMock())
    assert scorer.provider_name == "claude"


@pytest.mark.asyncio
async def test_claude_scorer_blocks_without_text_attr_returns_failure() -> None:
    """response.content has blocks but blocks[0] lacks 'text' → _failure returned (line 93->95)."""
    svc = MagicMock()
    response = MagicMock()
    block_no_text = MagicMock(spec=[])  # spec=[] → no attributes
    response.content = [block_no_text]
    svc._client.messages.create = AsyncMock(return_value=response)
    svc._model = "claude-test"

    scorer = ClaudeScorer(svc)
    result = await scorer.score("title", "body", "docs")

    assert result.classification is None
    assert result.provider.reasoning == "api_error"
