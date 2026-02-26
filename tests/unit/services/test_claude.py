from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinare.services.claude import ClaudeService


@pytest.mark.asyncio
async def test_assess_card_sufficiency_handles_empty_response() -> None:
    service = ClaudeService(api_key="test-key")

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is True


@pytest.mark.asyncio
async def test_assess_card_sufficiency_detects_false() -> None:
    service = ClaudeService(api_key="test-key")

    class _TextBlock:
        text = '{"sufficient": false}'

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[_TextBlock()])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is False


@pytest.mark.asyncio
async def test_assess_card_sufficiency_heuristic_fallback_sufficient() -> None:
    """Non-JSON response containing 'sufficient: true' triggers heuristic."""
    service = ClaudeService(api_key="test-key")

    class _TextBlock:
        text = 'The card looks good. "sufficient": true, all criteria met.'

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[_TextBlock()])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is True
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_assess_card_sufficiency_heuristic_fallback_insufficient() -> None:
    """Non-JSON response without 'sufficient: true' returns insufficient."""
    service = ClaudeService(api_key="test-key")

    class _TextBlock:
        text = "This card needs more details about the API."

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[_TextBlock()])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is False
    assert "clarify" in result["questions"][0].lower()
