from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.services.conducting import (
    AnthropicApiBackend,
    ClaudeCliBackend,
    CodexCliBackend,
    NullBackend,
    OpenAiApiBackend,
    OpenCodeBackend,
    _build_assess_prompt,
    _parse_assessment_response,
    _parse_prompt_response,
    build_conducting_backend,
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
# build_conducting_backend factory
# ---------------------------------------------------------------------------

def _cfg(backend: str) -> MagicMock:
    from coordinare.config import ConductingConfig
    cfg = MagicMock()
    # ConductingConfig validates backend against a Literal; for the "unknown"
    # test case we synthesize a duck-typed stand-in instead.
    try:
        cfg.conducting = ConductingConfig(backend=backend)  # type: ignore[arg-type]
    except Exception:
        fake = MagicMock()
        fake.backend = backend
        fake.model = None
        fake.max_tokens = 4096
        fake.temperature = None
        fake.executable = None
        fake.base_url = None
        cfg.conducting = fake
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


class TestBuildConductingBackend:
    def test_none_returns_null_backend(self) -> None:
        assert isinstance(build_conducting_backend(_cfg("none")), NullBackend)

    def test_claude_cli_returns_claude_cli_backend(self) -> None:
        assert isinstance(build_conducting_backend(_cfg("claude_cli")), ClaudeCliBackend)

    def test_opencode_returns_opencode_backend(self) -> None:
        assert isinstance(build_conducting_backend(_cfg("opencode")), OpenCodeBackend)

    def test_anthropic_api_returns_anthropic_api_backend(self) -> None:
        result = build_conducting_backend(_cfg("anthropic_api"))
        assert isinstance(result, AnthropicApiBackend)

    def test_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown conducting backend"):
            build_conducting_backend(_cfg("foobar"))

    def test_openai_api_returns_openai_backend(self) -> None:
        result = build_conducting_backend(_cfg("openai_api"))
        assert isinstance(result, OpenAiApiBackend)

    def test_codex_cli_returns_codex_cli_backend(self) -> None:
        result = build_conducting_backend(_cfg("codex_cli"))
        assert isinstance(result, CodexCliBackend)

    def test_anthropic_threads_conducting_config(self) -> None:
        from coordinare.config import ConductingConfig
        cfg = MagicMock()
        cfg.conducting = ConductingConfig(
            backend="anthropic_api", model="claude-test", max_tokens=2048, temperature=0.3
        )
        backend = build_conducting_backend(cfg)
        assert isinstance(backend, AnthropicApiBackend)
        assert backend._svc._model == "claude-test"
        assert backend._svc._max_tokens == 2048
        assert backend._svc._temperature == 0.3

    def test_factory_threads_effort_to_openai(self) -> None:
        from coordinare.config import ConductingConfig
        cfg = MagicMock()
        cfg.conducting = ConductingConfig(backend="openai_api", effort="high")
        backend = build_conducting_backend(cfg)
        assert isinstance(backend, OpenAiApiBackend)
        assert backend._effort == "high"

    def test_factory_threads_effort_to_codex_cli(self) -> None:
        from coordinare.config import ConductingConfig
        cfg = MagicMock()
        cfg.conducting = ConductingConfig(backend="codex_cli", effort="medium")
        backend = build_conducting_backend(cfg)
        assert isinstance(backend, CodexCliBackend)
        assert backend._effort == "medium"

    def test_factory_threads_effort_to_opencode(self) -> None:
        from coordinare.config import ConductingConfig
        cfg = MagicMock()
        cfg.conducting = ConductingConfig(backend="opencode", effort="low")
        backend = build_conducting_backend(cfg)
        assert isinstance(backend, OpenCodeBackend)
        assert backend._effort == "low"


def _fake_httpx_client(
    *,
    response_json: dict | None = None,
    raise_on_status: Exception | None = None,
    raise_on_post: Exception | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a MagicMock that quacks like ``httpx.AsyncClient`` for the post path."""
    fake_response = MagicMock()
    fake_response.status_code = status_code
    if raise_on_status is not None:
        fake_response.raise_for_status = MagicMock(side_effect=raise_on_status)
    else:
        fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value=response_json or {})
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    if raise_on_post is not None:
        fake_client.post = AsyncMock(side_effect=raise_on_post)
    else:
        fake_client.post = AsyncMock(return_value=fake_response)
    return fake_client


class TestOpenAiApiBackend:
    @pytest.mark.asyncio
    async def test_missing_key_returns_empty(self) -> None:
        backend = OpenAiApiBackend(api_key=None, model="gpt-test")
        result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False
        assert result["rationale"] == "empty openai response"

    @pytest.mark.asyncio
    async def test_parses_chat_completion(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(
            response_json={
                "choices": [
                    {"message": {"content": '{"sufficient": true, "questions": [], "rationale": "ok"}'}}
                ]
            }
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.assess({"title": "X", "body": "Y"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_http_4xx_returns_empty_response(self) -> None:
        import httpx
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        # raise_for_status raises HTTPStatusError, which is an httpx.HTTPError.
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(401, request=request)
        err = httpx.HTTPStatusError("unauthorized", request=request, response=response)
        client = _fake_httpx_client(raise_on_status=err, status_code=401)
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False
        assert result["rationale"] == "empty openai response"
        # 4xx is non-retryable.
        assert client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_http_5xx_returns_empty_response(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        # 5xx triggers the retry loop; helper returns 503 on every attempt so
        # the budget is exhausted. raise_for_status is never called because
        # _chat short-circuits on status_code before reaching it.
        client = _fake_httpx_client(status_code=503)
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False
        # 3 attempts (1 initial + 2 retries).
        assert client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_connection_error_returns_empty_response(self) -> None:
        import httpx
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(raise_on_post=httpx.ConnectError("dns"))
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False
        # TransportError is retried.
        assert client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_5xx_then_success(self) -> None:
        """Transient 5xx clears on retry → returned content is from the 2nd call."""
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")

        # Two responses: first 503, second 200 with a real chat completion.
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.raise_for_status = MagicMock()
        fail_resp.json = MagicMock(return_value={})

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json = MagicMock(return_value={
            "choices": [{"message": {"content": '{"sufficient": true, "questions": [], "rationale": "ok"}'}}]
        })

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=[fail_resp, ok_resp])

        with patch("httpx.AsyncClient", return_value=fake_client), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is True
        assert fake_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_429_then_success(self) -> None:
        """429 (rate-limit) is retried like 5xx."""
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")

        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.raise_for_status = MagicMock()
        fail_resp.json = MagicMock(return_value={})

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json = MagicMock(return_value={
            "choices": [{"message": {"content": '{"sufficient": false}'}}]
        })

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=[fail_resp, ok_resp])

        with patch("httpx.AsyncClient", return_value=fake_client), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            await backend.assess({"title": "X"})
        assert fake_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self) -> None:
        """4xx (other than 429) is not retried — authoritative client error."""
        import httpx
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(400, request=request)
        err = httpx.HTTPStatusError("bad request", request=request, response=response)
        client = _fake_httpx_client(raise_on_status=err, status_code=400)
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            await backend.assess({"title": "X"})
        assert client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_malformed_response_empty_choices(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(response_json={"choices": []})
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_malformed_response_missing_message_content(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(response_json={"choices": [{"message": {}}]})
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_malformed_response_non_dict_body(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(response_json=["not a dict"])  # type: ignore[arg-type]
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.assess({"title": "X"})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_payload_shape_chat_model(self) -> None:
        """Non-reasoning model: max_tokens + temperature, no reasoning_effort."""
        backend = OpenAiApiBackend(
            api_key="k", model="gpt-4o-mini", max_tokens=1234, temperature=0.7
        )
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": '{"sufficient": true}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            await backend.assess({"title": "X"})
        payload = client.post.call_args.kwargs["json"]
        assert payload["model"] == "gpt-4o-mini"
        assert payload["max_tokens"] == 1234
        assert payload["temperature"] == 0.7
        assert "max_completion_tokens" not in payload
        assert "reasoning_effort" not in payload
        assert payload["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_payload_shape_reasoning_model(self) -> None:
        """Reasoning model (effort set): max_completion_tokens + reasoning_effort,
        and temperature/max_tokens are omitted because the API rejects them."""
        backend = OpenAiApiBackend(
            api_key="k", model="o3-mini", max_tokens=2048, temperature=0.5, effort="high"
        )
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": '{"sufficient": true}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            await backend.assess({"title": "X"})
        payload = client.post.call_args.kwargs["json"]
        assert payload["max_completion_tokens"] == 2048
        assert payload["reasoning_effort"] == "high"
        assert "max_tokens" not in payload
        assert "temperature" not in payload

    @pytest.mark.asyncio
    async def test_base_url_override_strips_trailing_slash(self) -> None:
        backend = OpenAiApiBackend(
            api_key="k", model="gpt-test", base_url="https://proxy.example.com/v1/"
        )
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": '{"sufficient": true}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            await backend.assess({"title": "X"})
        url = client.post.call_args.args[0]
        assert url == "https://proxy.example.com/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_auth_header_present(self) -> None:
        backend = OpenAiApiBackend(api_key="sk-abc", model="gpt-test")
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": '{"sufficient": true}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            await backend.assess({"title": "X"})
        headers = client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-abc"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_prompt_method_success(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": "hello there"}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.prompt("say hi")
        assert result == {"text": "hello there", "data": None}

    @pytest.mark.asyncio
    async def test_prompt_method_json_response_format(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(
            response_json={"choices": [{"message": {"content": '{"answer": 42}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.prompt("Q?", response_format="json")
        assert result["data"] == {"answer": 42}
        payload = client.post.call_args.kwargs["json"]
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["messages"][0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_prompt_empty_short_circuits(self) -> None:
        backend = OpenAiApiBackend(api_key="k", model="gpt-test")
        client = _fake_httpx_client(response_json={})
        with patch("httpx.AsyncClient", return_value=client):
            result = await backend.prompt("   ")
        assert result == {"text": "", "data": None}
        client.post.assert_not_called()


class TestCodexCliBackend:
    @pytest.mark.asyncio
    async def test_empty_stdout_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"")):
            result = await CodexCliBackend().assess({})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_parses_stdout_json(self) -> None:
        out = b'{"sufficient": true, "questions": [], "rationale": "ok"}'
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(out)):
            result = await CodexCliBackend().assess({"title": "X"})
        assert result["sufficient"] is True

    def test_build_args_no_options(self) -> None:
        backend = CodexCliBackend(executable="codex")
        assert backend._build_args() == ["codex", "exec", "-"]

    def test_build_args_orders_options_before_stdin_sentinel(self) -> None:
        """Regression: `-` is the positional PROMPT for `codex exec` (read from
        stdin). Flags must precede it; the trailing `-` is the last argv element."""
        backend = CodexCliBackend(executable="codex", model="o3", effort="high")
        args = backend._build_args()
        assert args[-1] == "-"
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "o3"
        assert model_idx < args.index("-")
        assert "-c" in args
        c_idx = args.index("-c")
        # No embedded quotes around the value — codex parses as TOML with
        # literal-string fallback.
        assert args[c_idx + 1] == "model_reasoning_effort=high"
        assert c_idx < args.index("-")

    @pytest.mark.asyncio
    async def test_assess_pipes_prompt_via_stdin(self) -> None:
        captured = {}

        async def fake_communicate(*, input: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = input
            return (b'{"sufficient": true, "questions": [], "rationale": "ok"}', b"")

        proc = MagicMock()
        proc.communicate = fake_communicate
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await CodexCliBackend(executable="codex").assess({"title": "Card-X"})
        # argv should be [codex, exec, -] (no flags configured)
        assert mock_exec.call_args.args == ("codex", "exec", "-")
        assert b"Card-X" in captured["stdin"]
        # stdin must be wired up
        assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_returns_insufficient(self) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = -9
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await CodexCliBackend().assess({"title": "X"})
        assert result["sufficient"] is False
        assert result["rationale"] == "empty codex response"
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_oserror_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await CodexCliBackend().assess({"title": "X"})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_nonzero_exit_still_parses_stdout(self) -> None:
        """codex exec may exit non-zero (e.g. warning) but still produce a
        usable response on stdout; we should parse what we have and log."""
        proc = _make_proc(
            b'{"sufficient": true, "questions": [], "rationale": "ok"}', returncode=1
        )
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await CodexCliBackend().assess({"title": "X"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_prompt_pipes_text_via_stdin(self) -> None:
        captured = {}

        async def fake_communicate(*, input: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = input
            return (b'{"answer": 42}', b"")

        proc = MagicMock()
        proc.communicate = fake_communicate
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await CodexCliBackend().prompt("what?", response_format="json")
        assert captured["stdin"] == b"what?"
        assert result["data"] == {"answer": 42}

    @pytest.mark.asyncio
    async def test_prompt_empty_short_circuits(self) -> None:
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            result = await CodexCliBackend().prompt("   ")
        assert result == {"text": "", "data": None}
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_prompt_timeout_returns_empty(self) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = -9
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await CodexCliBackend().prompt("hello")
        assert result == {"text": "", "data": None}
        proc.kill.assert_called_once()


class TestClaudeCliBackendStdinAssess:
    """Regression: assess() must pipe prompts via stdin (not argv) so user
    content stays out of process listings and avoids argv length limits."""

    @pytest.mark.asyncio
    async def test_assess_uses_stdin_not_argv(self) -> None:
        captured = {}

        async def fake_communicate(*, input: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = input
            return (b'{"sufficient": true, "questions": [], "rationale": "ok"}', b"")

        proc = MagicMock()
        proc.communicate = fake_communicate
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await ClaudeCliBackend().assess({"title": "Secret-Card"})
        args = mock_exec.call_args.args
        # Prompt must NOT be a positional argv element
        assert not any("Secret-Card" in (a if isinstance(a, str) else "") for a in args)
        assert args[-1] == "-"
        assert b"Secret-Card" in captured["stdin"]


class TestOpenCodeBackendStdinAssess:
    @pytest.mark.asyncio
    async def test_assess_uses_stdin_not_argv(self) -> None:
        captured = {}

        async def fake_communicate(*, input: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = input
            return (b'{"sufficient": true, "questions": [], "rationale": "ok"}', b"")

        proc = MagicMock()
        proc.communicate = fake_communicate
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await OpenCodeBackend(executable="opencode", effort="medium").assess(
                {"title": "Secret-Card"}
            )
        args = mock_exec.call_args.args
        assert not any("Secret-Card" in (a if isinstance(a, str) else "") for a in args)
        assert args[-1] == "-"
        # effort args flow through
        assert "--effort" in args
        assert args[args.index("--effort") + 1] == "medium"
        assert b"Secret-Card" in captured["stdin"]


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
