"""Unit tests for ClaudeServiceLLMClient (spec 063 T026b)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import stamina
from anthropic import APIStatusError, APITimeoutError, AuthenticationError
from coordinare_service_inference.claude_llm_client import (
    SUBMIT_MANIFEST_TOOL,
    ClaudeServiceLLMClient,
    PermanentLLMError,
    TransientLLMError,
    _build_tool_catalogue,
)


def _block(type_: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kw)


def _response(content: list[Any], *, input_tokens: int = 0, output_tokens: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeMessages:
    """Stand-in for ``AsyncAnthropic().messages``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[Any] = []

    def queue(self, response: Any) -> None:
        self._queue.append(response)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


@pytest.fixture()
def fake_client() -> _FakeClient:
    return _FakeClient()


def _make_adapter(client: _FakeClient, **overrides: Any) -> ClaudeServiceLLMClient:
    defaults: dict[str, Any] = {
        "anthropic_client": client,
        "model": "claude-test",
        "max_tokens": 4096,
        "system_prompt": "you infer services",
        # Tests run synchronously fast — disable backoff so failure paths
        # don't bloat suite duration.
        "retry_kwargs": {
            "attempts": 3,
            "wait_initial": 0.001,
            "wait_max": 0.001,
            "wait_jitter": 0.0,
            "wait_exp_base": 1.0,
        },
    }
    defaults.update(overrides)
    return ClaudeServiceLLMClient(**defaults)


# --------------------------------------------------------------------- catalogue


def test_tool_catalogue_excludes_web_search_when_disabled():
    cat = _build_tool_catalogue(web_search_enabled=False)
    names = [t["name"] for t in cat]
    assert "web_search" not in names
    # The submit_manifest sentinel must always be present, otherwise the agent
    # can never terminate.
    assert names[-1] == SUBMIT_MANIFEST_TOOL


def test_tool_catalogue_includes_web_search_when_enabled():
    cat = _build_tool_catalogue(web_search_enabled=True)
    names = [t["name"] for t in cat]
    assert "web_search" in names
    assert names[-1] == SUBMIT_MANIFEST_TOOL


def test_submit_manifest_input_schema_is_the_manifest_schema():
    cat = _build_tool_catalogue(web_search_enabled=False)
    submit = next(t for t in cat if t["name"] == SUBMIT_MANIFEST_TOOL)
    # Smoke-check: the schema is a JSON-schema object with services as a
    # property. The full schema content is owned by schema.py — we just
    # verify the wire-up.
    assert submit["input_schema"]["type"] == "object"
    assert "services" in submit["input_schema"]["properties"]


# ----------------------------------------------------------------------- step()


@pytest.mark.asyncio
async def test_step_passes_system_and_tools_to_anthropic(fake_client):
    fake_client.messages.queue(_response([], input_tokens=10, output_tokens=20))
    adapter = _make_adapter(fake_client)

    await adapter.step([{"role": "user", "content": "go"}])

    assert len(fake_client.messages.calls) == 1
    call = fake_client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["system"] == "you infer services"
    assert call["messages"] == [{"role": "user", "content": "go"}]
    # The tool catalogue is computed once at construction and reused — this
    # ensures we don't accidentally drift between turns.
    assert any(t["name"] == SUBMIT_MANIFEST_TOOL for t in call["tools"])


@pytest.mark.asyncio
async def test_step_parses_tool_use_blocks_into_tool_calls(fake_client):
    fake_client.messages.queue(
        _response(
            [
                _block("text", text="thinking"),
                _block("tool_use", id="t1", name="read_file", input={"path": "Gemfile"}),
                _block("tool_use", id="t2", name="list_dir", input={"path": "config"}),
            ],
            input_tokens=100,
            output_tokens=50,
        )
    )
    adapter = _make_adapter(fake_client)

    result = await adapter.step([{"role": "user", "content": "go"}])

    assert result.manifest is None
    assert [c.name for c in result.tool_calls] == ["read_file", "list_dir"]
    assert result.tool_calls[0].arguments == {"path": "Gemfile"}
    assert result.input_tokens == 100
    assert result.output_tokens == 50


@pytest.mark.asyncio
async def test_step_routes_submit_manifest_to_manifest_field(fake_client):
    manifest_input = {
        "services": [
            {
                "name": "redis",
                "binary": "redis-server",
                "version": "7.2",
                "data_dir": "/tmp/redis",
                "port": 6379,
                "why_needed": "test",
                "sources": [],
            }
        ],
        "cache_inputs": [],
        "agent_version": "v1",
    }
    fake_client.messages.queue(
        _response(
            [_block("tool_use", id="m1", name=SUBMIT_MANIFEST_TOOL, input=manifest_input)],
            input_tokens=200,
            output_tokens=300,
        )
    )
    adapter = _make_adapter(fake_client)

    result = await adapter.step([{"role": "user", "content": "go"}])

    assert result.tool_calls == []
    assert result.manifest == manifest_input
    assert result.input_tokens == 200
    assert result.output_tokens == 300


@pytest.mark.asyncio
async def test_submit_manifest_wins_over_concurrent_tool_calls(fake_client):
    # Belt-and-suspenders: if the model mixes a final manifest with stray
    # tool calls, the strategy must surface the manifest so the agent loop
    # can terminate. Without this, we'd dispatch sandbox calls for a model
    # that's already decided it's done.
    manifest_input = {
        "services": [],
        "cache_inputs": [],
        "agent_version": "v1",
    }
    fake_client.messages.queue(
        _response(
            [
                _block("tool_use", id="t1", name="read_file", input={"path": "a"}),
                _block("tool_use", id="m1", name=SUBMIT_MANIFEST_TOOL, input=manifest_input),
            ]
        )
    )
    adapter = _make_adapter(fake_client)

    result = await adapter.step([{"role": "user", "content": "go"}])

    assert result.manifest == manifest_input
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_step_ignores_non_tool_use_blocks(fake_client):
    fake_client.messages.queue(_response([_block("text", text="hello")]))
    adapter = _make_adapter(fake_client)

    result = await adapter.step([{"role": "user", "content": "go"}])

    assert result.manifest is None
    assert result.tool_calls == []


@pytest.fixture()
def _stamina_active():
    # The project conftest disables stamina retries globally; retry-behavior
    # tests must opt back in for the duration of the test.
    stamina.set_active(True)
    yield
    stamina.set_active(False)


@pytest.mark.asyncio
async def test_step_retries_transient_timeout_then_succeeds(fake_client, _stamina_active):
    fake_client.messages.queue(APITimeoutError(request=None))  # type: ignore[arg-type]
    fake_client.messages.queue(_response([], input_tokens=1, output_tokens=2))
    adapter = _make_adapter(fake_client)

    result = await adapter.step([{"role": "user", "content": "go"}])

    assert len(fake_client.messages.calls) == 2
    assert result.input_tokens == 1


def _api_status_error(status_code: int) -> APIStatusError:
    return APIStatusError(
        message="boom",
        response=SimpleNamespace(status_code=status_code, headers={}, request=None),  # type: ignore[arg-type]
        body=None,
    )


@pytest.mark.asyncio
async def test_step_retries_5xx_then_gives_up(fake_client, _stamina_active):
    for _ in range(3):
        fake_client.messages.queue(_api_status_error(503))
    adapter = _make_adapter(fake_client)

    with pytest.raises(TransientLLMError):
        await adapter.step([{"role": "user", "content": "go"}])
    assert len(fake_client.messages.calls) == 3


@pytest.mark.asyncio
async def test_step_does_not_retry_auth_error(fake_client):
    fake_client.messages.queue(
        AuthenticationError(
            message="bad key",
            response=SimpleNamespace(status_code=401, headers={}, request=None),  # type: ignore[arg-type]
            body=None,
        )
    )
    adapter = _make_adapter(fake_client)

    with pytest.raises(PermanentLLMError):
        await adapter.step([{"role": "user", "content": "go"}])
    # Auth errors should fail fast — burning retries on them wastes time and
    # masks the operator-actionable cause.
    assert len(fake_client.messages.calls) == 1


@pytest.mark.asyncio
async def test_step_does_not_retry_4xx_status_error(fake_client):
    fake_client.messages.queue(_api_status_error(400))
    adapter = _make_adapter(fake_client)

    with pytest.raises(PermanentLLMError):
        await adapter.step([{"role": "user", "content": "go"}])
    assert len(fake_client.messages.calls) == 1
