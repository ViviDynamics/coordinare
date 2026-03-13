"""Coverage tests for ClaudeService error-handling paths."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import APIStatusError

from coordinare.services.claude import (
    ClaudeService,
    PermanentAnthropicError,
    TransientAnthropicError,
)


def _service() -> ClaudeService:
    return ClaudeService(api_key="test-key")


def _api_status_error(status_code: int) -> APIStatusError:
    response = httpx.Response(status_code, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return APIStatusError("error", response=response, body={})


@pytest.mark.asyncio
async def test_api_status_500_raises_transient() -> None:
    """5xx APIStatusError is wrapped as TransientAnthropicError."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(500))

    with pytest.raises(TransientAnthropicError):
        await svc.assess_card_sufficiency({"title": "T"})


@pytest.mark.asyncio
async def test_api_status_429_raises_transient() -> None:
    """429 APIStatusError is wrapped as TransientAnthropicError."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(429))

    with pytest.raises(TransientAnthropicError):
        await svc.assess_card_sufficiency({"title": "T"})


@pytest.mark.asyncio
async def test_api_status_400_raises_permanent() -> None:
    """4xx (non-429) APIStatusError is wrapped as PermanentAnthropicError."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(400))

    with pytest.raises(PermanentAnthropicError):
        await svc.assess_card_sufficiency({"title": "T"})


@pytest.mark.asyncio
async def test_assess_with_clarification_history_uses_history_prompt() -> None:
    """When clarifications are present, the service builds a history-aware prompt."""
    from types import SimpleNamespace

    svc = _service()
    create_mock = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(text='{"sufficient": true, "questions": [], "rationale": "enough info"}')]
    ))
    svc._client = SimpleNamespace(
        messages=type("M", (), {"create": create_mock})()
    )

    result = await svc.assess_card_sufficiency({
        "title": "Add badge",
        "clarifications": [{"questions": ["What CI?"], "answer": "GitHub Actions"}],
    })

    assert result["sufficient"] is True
    assert result["rationale"] == "enough info"
    # Verify the prompt sent to the API actually contains the clarification history
    call_kwargs = create_mock.call_args[1]
    prompt_text = call_kwargs["messages"][0]["content"]
    assert "What CI?" in prompt_text
    assert "GitHub Actions" in prompt_text


@pytest.mark.asyncio
async def test_assess_clarification_entry_with_no_questions_no_answer() -> None:
    """Clarification entries with empty questions/answer fields don't crash history building."""
    from types import SimpleNamespace

    svc = _service()
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"sufficient": true, "questions": [], "rationale": "ok"}')]
    )
    svc._client = SimpleNamespace(
        messages=type("M", (), {"create": AsyncMock(return_value=response)})()
    )

    result = await svc.assess_card_sufficiency({
        "title": "T",
        "clarifications": [
            {"questions": [], "answer": ""},       # both empty — branches hit but skipped
            {"questions": ["Scope?"], "answer": "All pages"},
        ],
    })

    assert result["sufficient"] is True


@pytest.mark.asyncio
async def test_block_without_text_attr_returns_empty_fallback() -> None:
    """Content block lacking a .text attribute falls through to empty-response fallback."""
    svc = _service()

    class _NoTextBlock:
        pass  # deliberately no .text attribute

    svc._client = SimpleNamespace(
        messages=type(
            "M",
            (),
            {"create": AsyncMock(return_value=SimpleNamespace(content=[_NoTextBlock()]))},
        )()
    )

    result = await svc.assess_card_sufficiency({"title": "T"})

    assert result["sufficient"] is True
    assert result["rationale"] == "empty model response"
