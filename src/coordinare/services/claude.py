from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic


class ClaudeService:
    def __init__(self, api_key: str | None = None, model: str = "claude-3-5-sonnet-latest") -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def assess_card_sufficiency(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Assess whether the card is sufficient for execution. "
            "Return JSON with fields: sufficient (bool), questions (list[str]), rationale (str)."
        )
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt}\nCard:\n{card}",
                }
            ],
        )
        text = ""
        if getattr(response, "content", None):
            blocks = response.content
            if blocks and hasattr(blocks[0], "text"):
                text = blocks[0].text
        lowered = text.lower()
        sufficient = "\"sufficient\": true" in lowered or "sufficient: true" in lowered
        if not text:
            return {"sufficient": True, "questions": [], "rationale": "empty model response"}
        if sufficient:
            return {"sufficient": True, "questions": [], "rationale": text}
        return {
            "sufficient": False,
            "questions": ["Please clarify acceptance criteria."],
            "rationale": text,
        }
