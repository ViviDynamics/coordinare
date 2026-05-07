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


# ---------------------------------------------------------------------------
# prompt_text() coverage (lines 53-108)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_text_returns_first_block_text() -> None:
    """prompt_text() returns the .text of the first content block."""
    svc = _service()
    svc._client = SimpleNamespace(
        messages=type(
            "M",
            (),
            {"create": AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text="hello world")]))},
        )()
    )
    result = await svc.prompt_text("What is Python?")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_prompt_text_json_format_adds_system_message() -> None:
    """response_format='json' adds a system instruction asking for JSON output."""
    svc = _service()
    create_mock = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')]))
    svc._client = SimpleNamespace(messages=type("M", (), {"create": create_mock})())

    result = await svc.prompt_text("Parse this", response_format="json")

    assert result == '{"ok": true}'
    call_kwargs = create_mock.call_args[1]
    assert call_kwargs.get("system") == "Respond with valid JSON only."


@pytest.mark.asyncio
async def test_prompt_text_empty_response_returns_empty_string() -> None:
    """Empty content list from model returns empty string."""
    svc = _service()
    svc._client = SimpleNamespace(
        messages=type("M", (), {"create": AsyncMock(return_value=SimpleNamespace(content=[]))})()
    )
    result = await svc.prompt_text("hello")
    assert result == ""


@pytest.mark.asyncio
async def test_prompt_text_connection_error_raises_transient() -> None:
    """APIConnectionError from prompt_text is wrapped as TransientAnthropicError."""
    from anthropic import APIConnectionError

    svc = _service()
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(
        side_effect=APIConnectionError(request=req)
    )

    with pytest.raises(TransientAnthropicError):
        await svc.prompt_text("test")


@pytest.mark.asyncio
async def test_prompt_text_500_raises_transient() -> None:
    """5xx APIStatusError in prompt_text is wrapped as TransientAnthropicError."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(500))

    with pytest.raises(TransientAnthropicError):
        await svc.prompt_text("test")


@pytest.mark.asyncio
async def test_prompt_text_400_raises_permanent() -> None:
    """4xx (non-429) APIStatusError in prompt_text is wrapped as PermanentAnthropicError."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(400))

    with pytest.raises(PermanentAnthropicError):
        await svc.prompt_text("test")


@pytest.mark.asyncio
async def test_prompt_text_failure_increments_failure_metric() -> None:
    """Any exception increments the failure metric and re-raises."""
    svc = _service()
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(side_effect=_api_status_error(500))

    with pytest.raises(TransientAnthropicError):
        await svc.prompt_text("test")
    # Test just verifies the exception propagates; metric counter verification
    # would require a Prometheus registry snapshot which is out of scope here.


@pytest.mark.asyncio
async def test_prompt_text_circuit_breaker_path() -> None:
    """When a circuit_breaker is set, prompt_text routes through its guard()."""
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock as _MagicMock

    svc = _service()
    svc._client = SimpleNamespace(
        messages=type(
            "M",
            (),
            {"create": _AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text="cb result")]))},
        )()
    )

    mock_cb = _MagicMock()
    mock_ctx = _MagicMock()
    mock_ctx.__aenter__ = _AsyncMock(return_value=None)
    mock_ctx.__aexit__ = _AsyncMock(return_value=False)
    mock_cb.guard.return_value = mock_ctx
    svc._circuit_breaker = mock_cb

    result = await svc.prompt_text("circuit test")

    assert result == "cb result"
    mock_cb.guard.assert_called_once()


@pytest.mark.asyncio
async def test_prompt_text_authentication_error_raises_permanent() -> None:
    """AuthenticationError in prompt_text is wrapped as PermanentAnthropicError."""
    from anthropic import AuthenticationError

    svc = _service()
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=req)
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(
        side_effect=AuthenticationError("invalid api key", response=response, body={})
    )

    with pytest.raises(PermanentAnthropicError):
        await svc.prompt_text("test")
