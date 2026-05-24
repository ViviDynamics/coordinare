"""Unit tests for OpenAICompatServiceLLMClient (spec 069 Phase 2)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import stamina
from coordinare_service_inference.claude_llm_client import (
    SUBMIT_MANIFEST_TOOL,
    PermanentLLMError,
    TransientLLMError,
)
from coordinare_service_inference.openai_compat_llm_client import (
    OpenAICompatServiceLLMClient,
    _anthropic_messages_to_openai,
    _anthropic_tools_to_openai,
    _parse_openai_response,
)

# ------------------------------------------------------------------ translators


def test_anthropic_tools_to_openai_round_trips_schema():
    tools = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    out = _anthropic_tools_to_openai(tools)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


def test_anthropic_messages_translation_prepends_system_and_flattens_tool_calls():
    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file body"},
            ],
        },
    ]
    out = _anthropic_messages_to_openai(msgs, "you infer services")
    assert out[0] == {"role": "system", "content": "you infer services"}
    assert out[1] == {"role": "user", "content": "go"}
    assert out[2]["role"] == "assistant"
    assert out[2]["content"] == "thinking"
    assert out[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "a"}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "file body"}


def test_tool_result_dict_payload_is_json_encoded():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": {"k": "v"}},
            ],
        },
    ]
    out = _anthropic_messages_to_openai(msgs, "")
    assert out[0]["role"] == "tool"
    assert json.loads(out[0]["content"]) == {"k": "v"}


# ----------------------------------------------------------------- response parse


def _openai_response(
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": None}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _fn_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_parse_extracts_tool_calls_and_usage():
    payload = _openai_response(
        [_fn_call("c1", "read_file", {"path": "a"}), _fn_call("c2", "list_dir", {"path": "."})],
        prompt_tokens=10,
        completion_tokens=20,
    )
    step = _parse_openai_response(payload)
    assert step.manifest is None
    assert [c.name for c in step.tool_calls] == ["read_file", "list_dir"]
    assert step.tool_calls[0].arguments == {"path": "a"}
    assert step.input_tokens == 10
    assert step.output_tokens == 20


def test_parse_routes_submit_manifest_to_manifest_field():
    manifest = {"services": [], "cache_inputs": [], "agent_version": "v1"}
    payload = _openai_response([_fn_call("m1", SUBMIT_MANIFEST_TOOL, manifest)])
    step = _parse_openai_response(payload)
    assert step.manifest == manifest
    assert step.tool_calls == []


def test_parse_submit_manifest_wins_over_concurrent_calls():
    manifest = {"services": [], "cache_inputs": [], "agent_version": "v1"}
    payload = _openai_response(
        [
            _fn_call("c1", "read_file", {"path": "a"}),
            _fn_call("m1", SUBMIT_MANIFEST_TOOL, manifest),
        ]
    )
    step = _parse_openai_response(payload)
    assert step.manifest == manifest
    assert step.tool_calls == []


def test_parse_handles_malformed_arguments_as_empty():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "not-json"},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    step = _parse_openai_response(payload)
    assert step.tool_calls[0].arguments == {}


def test_parse_empty_choices_returns_empty_step():
    step = _parse_openai_response({"choices": [], "usage": {}})
    assert step.manifest is None
    assert step.tool_calls == []


def test_parse_unwraps_parameter_envelope_on_submit_manifest():
    # Qwen-via-LiteLLM wraps tool args in {"parameter": {...}}. Regression:
    # without unwrapping, ManifestValidationError fires with
    # "agent_version Field required" because the real fields sit one level deeper.
    manifest = {"services": [], "cache_inputs": [], "agent_version": "v1"}
    payload = _openai_response([_fn_call("m1", SUBMIT_MANIFEST_TOOL, {"parameter": manifest})])
    step = _parse_openai_response(payload)
    assert step.manifest == manifest


def test_parse_unwraps_parameters_envelope_on_tool_call():
    payload = _openai_response(
        [_fn_call("c1", "read_file", {"parameters": {"path": "Gemfile"}})]
    )
    step = _parse_openai_response(payload)
    assert step.tool_calls[0].arguments == {"path": "Gemfile"}


def test_parse_does_not_unwrap_legitimate_single_key_payload():
    # A real tool argument named "path" must NOT be unwrapped — only the
    # specific envelope keys (parameter/parameters/arguments) trigger unwrap.
    payload = _openai_response([_fn_call("c1", "read_file", {"path": "Gemfile"})])
    step = _parse_openai_response(payload)
    assert step.tool_calls[0].arguments == {"path": "Gemfile"}


def test_parse_does_not_unwrap_when_envelope_value_is_not_dict():
    # Defensive: if the model emits {"parameter": "not-a-dict"}, leave it
    # alone so the sandbox argument validator surfaces a precise error.
    payload = _openai_response([_fn_call("c1", "read_file", {"parameter": "Gemfile"})])
    step = _parse_openai_response(payload)
    assert step.tool_calls[0].arguments == {"parameter": "Gemfile"}


# -------------------------------------------------------------------- step() E2E


def _client_with_handler(handler) -> OpenAICompatServiceLLMClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OpenAICompatServiceLLMClient(
        http_client=http,
        base_url="http://litellm.test/v1",
        model="local-model",
        max_tokens=4096,
        system_prompt="you infer services",
        api_key="local-key",
        retry_kwargs={
            "attempts": 3,
            "wait_initial": 0.001,
            "wait_max": 0.001,
            "wait_jitter": 0.0,
            "wait_exp_base": 1.0,
        },
    )


@pytest.mark.asyncio
async def test_step_posts_to_chat_completions_with_auth_and_tools():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_openai_response([], prompt_tokens=1, completion_tokens=2))

    client = _client_with_handler(handler)
    await client.step([{"role": "user", "content": "go"}])

    assert captured["url"] == "http://litellm.test/v1/chat/completions"
    assert captured["auth"] == "Bearer local-key"
    body = captured["body"]
    assert body["model"] == "local-model"
    assert body["max_tokens"] == 4096
    assert body["messages"][0] == {"role": "system", "content": "you infer services"}
    assert any(
        t["function"]["name"] == SUBMIT_MANIFEST_TOOL for t in body["tools"]
    )


@pytest.mark.asyncio
async def test_step_no_auth_header_when_api_key_missing():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_openai_response([]))

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    client = OpenAICompatServiceLLMClient(
        http_client=http,
        base_url="http://litellm.test/v1",
        model="local-model",
        max_tokens=4096,
        system_prompt="sys",
        api_key=None,
    )
    await client.step([{"role": "user", "content": "go"}])
    assert captured["auth"] is None


@pytest.fixture()
def _stamina_active():
    stamina.set_active(True)
    yield
    stamina.set_active(False)


@pytest.mark.asyncio
async def test_step_retries_5xx_then_gives_up(_stamina_active):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    client = _client_with_handler(handler)
    with pytest.raises(TransientLLMError):
        await client.step([{"role": "user", "content": "go"}])
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_step_retries_429_then_succeeds(_stamina_active):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_openai_response([], prompt_tokens=5, completion_tokens=6))

    client = _client_with_handler(handler)
    result = await client.step([{"role": "user", "content": "go"}])
    assert state["n"] == 2
    assert result.input_tokens == 5


@pytest.mark.asyncio
async def test_step_does_not_retry_auth_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    client = _client_with_handler(handler)
    with pytest.raises(PermanentLLMError):
        await client.step([{"role": "user", "content": "go"}])
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_step_does_not_retry_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = _client_with_handler(handler)
    with pytest.raises(PermanentLLMError):
        await client.step([{"role": "user", "content": "go"}])
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_step_retries_connection_error(_stamina_active):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=_openai_response([]))

    client = _client_with_handler(handler)
    await client.step([{"role": "user", "content": "go"}])
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_stripped():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_openai_response([]))

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    client = OpenAICompatServiceLLMClient(
        http_client=http,
        base_url="http://litellm.test/v1/",
        model="local-model",
        max_tokens=4096,
        system_prompt="sys",
    )
    await client.step([{"role": "user", "content": "go"}])
    assert captured["url"] == "http://litellm.test/v1/chat/completions"
