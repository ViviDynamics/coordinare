from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinare.services.claude import ClaudeService, _try_parse_json_object


class TestTryParseJsonObject:
    def test_empty_input_returns_none(self) -> None:
        assert _try_parse_json_object("") is None
        assert _try_parse_json_object("   ") is None

    def test_exact_json_object(self) -> None:
        assert _try_parse_json_object('{"a": 1}') == {"a": 1}

    def test_code_fence_wrapped(self) -> None:
        text = 'Here you go:\n```json\n{"sufficient": true, "n": 2}\n```\nthanks'
        assert _try_parse_json_object(text) == {"sufficient": True, "n": 2}

    def test_code_fence_without_language(self) -> None:
        text = 'prefix ```\n{"x": "y"}\n``` suffix'
        assert _try_parse_json_object(text) == {"x": "y"}

    def test_prose_preamble_with_brace(self) -> None:
        text = 'The result is {"sufficient": false}.'
        assert _try_parse_json_object(text) == {"sufficient": False}

    def test_returns_none_for_json_array(self) -> None:
        # Array is valid JSON but not a dict — must not be returned
        assert _try_parse_json_object("[1, 2, 3]") is None

    def test_repairs_truncated_string(self) -> None:
        # Odd quote count → repair appends closing quote, then closing brace
        text = '{"reason": "card is missing acceptance'
        result = _try_parse_json_object(text)
        assert result == {"reason": "card is missing acceptance"}

    def test_repairs_truncated_braces(self) -> None:
        text = '{"outer": {"inner": 1}'
        result = _try_parse_json_object(text)
        assert result == {"outer": {"inner": 1}}

    def test_unrepairable_garbage(self) -> None:
        assert _try_parse_json_object("not json at all, no braces here") is None

    def test_unrepairable_starts_with_brace(self) -> None:
        # Starts with { but contents unrecoverable
        assert _try_parse_json_object("{this is :: not json @@") is None

    def test_repairs_truncated_array(self) -> None:
        text = '{"questions": ["a", "b'
        result = _try_parse_json_object(text)
        assert result == {"questions": ["a", "b"]}

    def test_repairs_nested_array_and_object(self) -> None:
        text = '{"a": {"b": [1, 2'
        result = _try_parse_json_object(text)
        assert result == {"a": {"b": [1, 2]}}

    def test_escaped_quotes_in_string_dont_confuse_balancer(self) -> None:
        # Embedded escaped quotes inside a complete string must NOT be
        # interpreted as unbalanced — text is already valid JSON.
        text = '{"rationale": "User said \\"yes\\""}'
        result = _try_parse_json_object(text)
        assert result == {"rationale": 'User said "yes"'}

    def test_trailing_comma_in_truncated_object(self) -> None:
        # `{"a": 1,` is truncated after the comma; repair must strip the
        # dangling comma rather than emit `{"a": 1,}` which is invalid.
        text = '{"a": 1,'
        result = _try_parse_json_object(text)
        assert result == {"a": 1}

    def test_trailing_colon_unrepairable(self) -> None:
        # `{"a":` would need a fabricated value — refuse the repair.
        text = '{"a":'
        assert _try_parse_json_object(text) is None

    def test_truncation_inside_escape_sequence(self) -> None:
        # `"foo\` — truncation landed on a backslash. Repair must drop the
        # backslash so the closing quote isn't interpreted as `\"`.
        text = '{"a": "foo\\'
        result = _try_parse_json_object(text)
        assert result == {"a": "foo"}

    def test_mismatched_brackets_unrepairable(self) -> None:
        # `{"a": [1, 2}` — the `}` closes the array's parent before the array
        # itself is closed. Repair must refuse rather than emit garbage.
        text = '{"a": [1, 2}'
        assert _try_parse_json_object(text) is None

    def test_brace_inside_string_doesnt_open_container(self) -> None:
        text = '{"a": "value with } and { inside"'
        result = _try_parse_json_object(text)
        assert result == {"a": "value with } and { inside"}


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
    """Non-JSON response blocks the card so the operator can inspect it."""
    service = ClaudeService(api_key="test-key")

    class _TextBlock:
        text = 'The card looks good. "sufficient": true, all criteria met.'

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[_TextBlock()])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is False
    assert result["questions"] == ["assessment parse error — model returned non-JSON"]
    assert "assessment parse error" in result["rationale"]


@pytest.mark.asyncio
async def test_assess_card_sufficiency_heuristic_fallback_non_json() -> None:
    """Non-JSON response blocks the card rather than silently dispatching it."""
    service = ClaudeService(api_key="test-key")

    class _TextBlock:
        text = "This card needs more details about the API."

    class _Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[_TextBlock()])

    service._client = SimpleNamespace(messages=_Messages())

    result = await service.assess_card_sufficiency({"title": "Card"})

    assert result["sufficient"] is False
    assert result["questions"] == ["assessment parse error — model returned non-JSON"]
    assert "This card needs more details" in result["rationale"]
