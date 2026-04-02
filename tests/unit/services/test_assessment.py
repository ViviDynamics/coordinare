from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.services.assessment import (
    AnthropicApiBackend,
    ClaudeCliBackend,
    NullBackend,
    OpenCodeBackend,
    _build_assess_prompt,
    _parse_assessment_response,
    _parse_prompt_response,
    build_assessment_backend,
)

# ---------------------------------------------------------------------------
# NullBackend
# ---------------------------------------------------------------------------

class TestNullBackend:
    @pytest.mark.asyncio
    async def test_always_sufficient(self) -> None:
        result = await NullBackend().assess({"title": "anything"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_rationale(self) -> None:
        result = await NullBackend().assess({})
        assert result["rationale"] == "assessment disabled"

    @pytest.mark.asyncio
    async def test_empty_questions(self) -> None:
        result = await NullBackend().assess({})
        assert result["questions"] == []


# ---------------------------------------------------------------------------
# AnthropicApiBackend
# ---------------------------------------------------------------------------

class TestAnthropicApiBackend:
    @pytest.mark.asyncio
    async def test_delegates_to_claude_service(self) -> None:
        expected = {"sufficient": True, "questions": [], "rationale": "ok"}
        svc = MagicMock()
        svc.assess_card_sufficiency = AsyncMock(return_value=expected)
        backend = AnthropicApiBackend(svc)

        result = await backend.assess({"title": "card"})

        svc.assess_card_sufficiency.assert_awaited_once_with({"title": "card"})
        assert result == expected

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self) -> None:
        svc = MagicMock()
        svc.assess_card_sufficiency = AsyncMock(side_effect=RuntimeError("api down"))
        backend = AnthropicApiBackend(svc)

        with pytest.raises(RuntimeError, match="api down"):
            await backend.assess({})


# ---------------------------------------------------------------------------
# ClaudeCliBackend
# ---------------------------------------------------------------------------

def _make_proc(stdout: bytes, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


class TestClaudeCliBackend:
    @pytest.mark.asyncio
    async def test_successful_json_response(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "good"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await ClaudeCliBackend().assess({"title": "card"})
        assert result["sufficient"] is True
        assert result["rationale"] == "good"

    @pytest.mark.asyncio
    async def test_heuristic_fallback_sufficient(self) -> None:
        text = b'"sufficient": true, everything looks good'
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(text)):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_heuristic_fallback_insufficient(self) -> None:
        text = b"Needs more detail in the acceptance criteria"
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(text)):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"")):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_timeout_returns_insufficient(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False
        assert "timeout" in result["rationale"]

    @pytest.mark.asyncio
    async def test_oserror_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False
        assert "cli error" in result["rationale"]

    @pytest.mark.asyncio
    async def test_correct_cli_args(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)) as mock_exec:
            await ClaudeCliBackend(executable="claude").assess({"title": "t"})
        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert args[1] == "--print"


# ---------------------------------------------------------------------------
# OpenCodeBackend
# ---------------------------------------------------------------------------

class TestOpenCodeBackend:
    @pytest.mark.asyncio
    async def test_successful_json_response(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await OpenCodeBackend().assess({"title": "card"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_correct_cli_args(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)) as mock_exec:
            await OpenCodeBackend(executable="opencode").assess({"title": "t"})
        args = mock_exec.call_args[0]
        assert args[0] == "opencode"
        assert args[1] == "run"
        # prompt is passed as the next positional arg (no --print flag)
        assert args[2] != "--print"

    @pytest.mark.asyncio
    async def test_timeout_returns_insufficient(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await OpenCodeBackend().assess({})
        assert result["sufficient"] is False
        assert "timeout" in result["rationale"]

    @pytest.mark.asyncio
    async def test_oserror_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await OpenCodeBackend().assess({})
        assert result["sufficient"] is False


# ---------------------------------------------------------------------------
# build_assessment_backend factory
# ---------------------------------------------------------------------------

def _cfg(backend: str) -> MagicMock:
    cfg = MagicMock()
    cfg.assessment_backend = backend
    return cfg


# ---------------------------------------------------------------------------
# _build_assess_prompt
# ---------------------------------------------------------------------------

class TestBuildAssessPrompt:
    def test_includes_title_and_description(self) -> None:
        prompt = _build_assess_prompt({"title": "Add badge", "body": "Put a CI badge in README"})
        assert "Add badge" in prompt
        assert "Put a CI badge" in prompt

    def test_includes_url_when_present(self) -> None:
        prompt = _build_assess_prompt({"title": "T", "url": "https://github.com/org/repo/issues/1"})
        assert "https://github.com/org/repo/issues/1" in prompt

    def test_includes_labels_dict_nodes_format(self) -> None:
        card = {
            "title": "T",
            "labels": {"nodes": [{"name": "bug"}, {"name": "enhancement"}]},
        }
        prompt = _build_assess_prompt(card)
        assert "bug" in prompt
        assert "enhancement" in prompt

    def test_includes_labels_list_format(self) -> None:
        prompt = _build_assess_prompt({"title": "T", "labels": ["urgent", "backend"]})
        assert "urgent" in prompt
        assert "backend" in prompt

    def test_includes_human_comments(self) -> None:
        card = {
            "title": "T",
            "comments": {
                "nodes": [
                    {"body": "Can we scope this to v2 only?", "author": {"login": "alice"}},
                ]
            },
        }
        prompt = _build_assess_prompt(card)
        assert "Can we scope this to v2 only?" in prompt
        assert "alice" in prompt

    def test_excludes_coordinare_needs_input_comments(self) -> None:
        card = {
            "title": "T",
            "comments": {
                "nodes": [
                    {"body": "Needs input:\n- What routes?", "author": {"login": "coordinare-bot"}},
                ]
            },
        }
        prompt = _build_assess_prompt(card)
        assert "coordinare-bot" not in prompt

    def test_skips_non_dict_comment_nodes(self) -> None:
        card = {
            "title": "T",
            "comments": {"nodes": ["not a dict", None, {"body": "valid comment", "author": {"login": "bob"}}]},
        }
        # Should not raise; valid comment is still included
        prompt = _build_assess_prompt(card)
        assert "valid comment" in prompt

    def test_clarification_entry_with_no_questions_no_answer(self) -> None:
        """Clarification entries with empty questions/answer should not crash."""
        card = {
            "title": "T",
            "clarifications": [
                {"questions": [], "answer": ""},       # both empty
                {"questions": ["What color?"], "answer": "Blue"},  # normal
            ],
        }
        prompt = _build_assess_prompt(card)
        assert "What color?" in prompt
        assert "Blue" in prompt

    def test_with_clarifications_builds_history_prompt(self) -> None:
        card = {
            "title": "T",
            "clarifications": [
                {"questions": ["What routes?"], "answer": "All routes"},
            ],
        }
        prompt = _build_assess_prompt(card)
        assert "What routes?" in prompt
        assert "All routes" in prompt
        assert "sufficient" in prompt.lower()

    def test_description_fallback_from_body_or_description_key(self) -> None:
        prompt_body = _build_assess_prompt({"title": "T", "body": "from body"})
        prompt_desc = _build_assess_prompt({"title": "T", "description": "from description"})
        assert "from body" in prompt_body
        assert "from description" in prompt_desc


# ---------------------------------------------------------------------------
# _parse_assessment_response
# ---------------------------------------------------------------------------

class TestParseAssessmentResponse:
    def test_parses_plain_json(self) -> None:
        text = '{"sufficient": true, "questions": [], "rationale": "good"}'
        result = _parse_assessment_response(text)
        assert result["sufficient"] is True

    def test_strips_markdown_code_fences(self) -> None:
        text = "```json\n{\"sufficient\": false, \"questions\": [\"Why?\"], \"rationale\": \"missing\"}\n```"
        result = _parse_assessment_response(text)
        assert result["sufficient"] is False
        assert result["questions"] == ["Why?"]

    def test_extracts_json_from_prose(self) -> None:
        text = 'Here is my assessment: {"sufficient": true, "questions": [], "rationale": "ok"} done.'
        result = _parse_assessment_response(text)
        assert result["sufficient"] is True

    def test_json_without_sufficient_key_falls_through_to_heuristic(self) -> None:
        # Valid JSON but no "sufficient" key — should fall through all strategies to heuristic
        text = '{"not_sufficient": true}'
        result = _parse_assessment_response(text)
        # heuristic: no "sufficient: true" keyword → insufficient
        assert result["sufficient"] is False

    def test_heuristic_true_from_keyword(self) -> None:
        result = _parse_assessment_response('"sufficient": true, all good')
        assert result["sufficient"] is True

    def test_heuristic_false_fallback(self) -> None:
        result = _parse_assessment_response("Needs more information about the scope.")
        assert result["sufficient"] is False


# ---------------------------------------------------------------------------
# OpenCodeBackend — empty stdout
# ---------------------------------------------------------------------------

class TestOpenCodeBackendEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_stdout_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"")):
            result = await OpenCodeBackend().assess({})
        assert result["sufficient"] is False
        assert "empty" in result["rationale"]


class TestBuildAssessmentBackend:
    def test_none_returns_null_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("none")), NullBackend)

    def test_claude_cli_returns_claude_cli_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("claude_cli")), ClaudeCliBackend)

    def test_opencode_returns_opencode_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("opencode")), OpenCodeBackend)

    def test_anthropic_api_returns_anthropic_api_backend(self) -> None:
        result = build_assessment_backend(_cfg("anthropic_api"))
        assert isinstance(result, AnthropicApiBackend)

    def test_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown assessment_backend"):
            build_assessment_backend(_cfg("foobar"))


# ---------------------------------------------------------------------------
# _parse_prompt_response
# ---------------------------------------------------------------------------

class TestParsePromptResponse:
    def test_plain_text_no_json(self) -> None:
        result = _parse_prompt_response("hello world", None)
        assert result == {"text": "hello world", "data": None}

    def test_json_format_valid_json(self) -> None:
        result = _parse_prompt_response('{"key": "value"}', "json")
        assert result["text"] == '{"key": "value"}'
        assert result["data"] == {"key": "value"}

    def test_json_format_invalid_json(self) -> None:
        result = _parse_prompt_response("not json", "json")
        assert result["text"] == "not json"
        assert result["data"] is None

    def test_json_format_code_fenced_json(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        result = _parse_prompt_response(text, "json")
        assert result["data"] == {"key": "value"}

    def test_json_format_empty_text(self) -> None:
        result = _parse_prompt_response("", "json")
        assert result == {"text": "", "data": None}

    def test_json_format_array(self) -> None:
        result = _parse_prompt_response('[1, 2, 3]', "json")
        assert result["data"] == [1, 2, 3]

    def test_no_format_skips_json_parse(self) -> None:
        result = _parse_prompt_response('{"key": "value"}', None)
        assert result["data"] is None


# ---------------------------------------------------------------------------
# NullBackend.prompt()
# ---------------------------------------------------------------------------

class TestNullBackendPrompt:
    @pytest.mark.asyncio
    async def test_returns_empty_text(self) -> None:
        result = await NullBackend().prompt("anything")
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_with_json_format(self) -> None:
        result = await NullBackend().prompt("anything", response_format="json")
        assert result == {"text": "", "data": None}


# ---------------------------------------------------------------------------
# AnthropicApiBackend.prompt()
# ---------------------------------------------------------------------------

class TestAnthropicApiBackendPrompt:
    @pytest.mark.asyncio
    async def test_delegates_to_prompt_text(self) -> None:
        svc = MagicMock()
        svc.prompt_text = AsyncMock(return_value="model says hello")
        backend = AnthropicApiBackend(svc)

        result = await backend.prompt("say hello")

        svc.prompt_text.assert_awaited_once_with("say hello", response_format=None)
        assert result["text"] == "model says hello"
        assert result["data"] is None

    @pytest.mark.asyncio
    async def test_json_response_format(self) -> None:
        svc = MagicMock()
        svc.prompt_text = AsyncMock(return_value='{"answer": 42}')
        backend = AnthropicApiBackend(svc)

        result = await backend.prompt("what is the answer?", response_format="json")

        svc.prompt_text.assert_awaited_once_with("what is the answer?", response_format="json")
        assert result["data"] == {"answer": 42}

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_immediately(self) -> None:
        svc = MagicMock()
        svc.prompt_text = AsyncMock()
        backend = AnthropicApiBackend(svc)

        result = await backend.prompt("")

        svc.prompt_text.assert_not_awaited()
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_whitespace_only_prompt_returns_immediately(self) -> None:
        svc = MagicMock()
        svc.prompt_text = AsyncMock()
        backend = AnthropicApiBackend(svc)

        result = await backend.prompt("   ")

        svc.prompt_text.assert_not_awaited()
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self) -> None:
        svc = MagicMock()
        svc.prompt_text = AsyncMock(side_effect=RuntimeError("api down"))
        backend = AnthropicApiBackend(svc)

        with pytest.raises(RuntimeError, match="api down"):
            await backend.prompt("hello")


# ---------------------------------------------------------------------------
# ClaudeCliBackend.prompt()
# ---------------------------------------------------------------------------

class TestClaudeCliBackendPrompt:
    @pytest.mark.asyncio
    async def test_successful_prompt(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"hello from claude")):
            result = await ClaudeCliBackend().prompt("say hello")
        assert result["text"] == "hello from claude"
        assert result["data"] is None

    @pytest.mark.asyncio
    async def test_json_response_format(self) -> None:
        payload = b'{"answer": 42}'
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await ClaudeCliBackend().prompt("what?", response_format="json")
        assert result["data"] == {"answer": 42}

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_immediately(self) -> None:
        result = await ClaudeCliBackend().prompt("")
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await ClaudeCliBackend().prompt("hello")
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_oserror_returns_empty(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await ClaudeCliBackend().prompt("hello")
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_prompt_uses_stdin(self) -> None:
        """Prompt is passed via stdin (not argv) to avoid process listing exposure."""
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"ok")) as mock_exec:
            await ClaudeCliBackend(executable="claude").prompt("test prompt")
        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert args[1] == "--print"
        assert "test prompt" not in args
        kwargs = mock_exec.call_args[1]
        assert kwargs.get("stdin") == asyncio.subprocess.PIPE


# ---------------------------------------------------------------------------
# OpenCodeBackend.prompt()
# ---------------------------------------------------------------------------

class TestOpenCodeBackendPrompt:
    @pytest.mark.asyncio
    async def test_successful_prompt(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"hello from opencode")):
            result = await OpenCodeBackend().prompt("say hello")
        assert result["text"] == "hello from opencode"

    @pytest.mark.asyncio
    async def test_json_response_format(self) -> None:
        payload = b'[{"concern": "security", "confidence": 0.9}]'
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await OpenCodeBackend().prompt("classify", response_format="json")
        assert result["data"] == [{"concern": "security", "confidence": 0.9}]

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_immediately(self) -> None:
        result = await OpenCodeBackend().prompt("")
        assert result == {"text": "", "data": None}

    @pytest.mark.asyncio
    async def test_prompt_uses_stdin(self) -> None:
        """Prompt is passed via stdin (not argv) to avoid process listing exposure."""
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"ok")) as mock_exec:
            await OpenCodeBackend(executable="opencode").prompt("test prompt")
        args = mock_exec.call_args[0]
        assert args[0] == "opencode"
        assert args[1] == "run"
        assert "test prompt" not in args
        kwargs = mock_exec.call_args[1]
        assert kwargs.get("stdin") == asyncio.subprocess.PIPE

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await OpenCodeBackend().prompt("hello")
        assert result == {"text": "", "data": None}
