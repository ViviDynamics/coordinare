"""223: route Claude Code around the gateway's lossy Anthropic adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from performer.config import Settings
from performer.main import _launch_dispatch_proxy
from performer.models import Score, Stand
from performer.proxy import launch
from performer.proxy.health import check_health
from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import SelfHostedShim

MODEL = "gateway/glm-5.3-flash"
BASE_URL = "http://litellm:4000/v1"


def _target(strategy: str = "translate") -> dict[str, Any]:
    return {
        "base_url": BASE_URL if strategy == "translate" else "http://litellm:4000",
        "wire_format": "openai" if strategy == "translate" else "anthropic",
        "strategy": strategy,
        "upstream_auth_env": "ANTHROPIC_AUTH_TOKEN",
        "health_probe": "completion",
    }


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    body = {
        "id": "issue-223",
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return ("data: " + json.dumps(body) + "\n\n").encode()


def _events(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: ")]


@pytest.mark.parametrize("reasoning_key", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("normalizers", [[], ["strip_control_chars", "strip_reasoning"]])
def test_mixed_reasoning_delta_keeps_json_prefix_without_empty_leading_block(
    reasoning_key: str,
    normalizers: list[str],
) -> None:
    shim = SelfHostedShim(
        target=TargetDescriptor.model_validate(_target()),
        normalizers=[NORMALIZER_REGISTRY[key] for key in normalizers],
    )
    chain = shim.sse_chain()
    before_text = b"".join(
        chain.feed(_chunk(delta))
        for delta in [
            {"role": "assistant", "content": ""},
            {reasoning_key: '"\n\nOutput', "content": None},
            {reasoning_key: " strictly", "content": None},
            {reasoning_key: " JSON,", "content": None},
        ]
    )
    assert all(event["type"] != "content_block_start" for event in _events(before_text))

    mixed = _chunk({reasoning_key: " no fences.", "content": '{"'})
    # A network read may split the mixed delta anywhere, even inside its JSON.
    out = before_text + chain.feed(mixed[:37]) + chain.feed(mixed[37:])
    out += chain.feed(_chunk({"content": 'sufficient": true'}))
    out += chain.feed(_chunk({"content": ', "questions": [], "dependencies": []}'}))
    out += chain.feed(_chunk({}, "stop"))
    out += chain.feed(b"data: [DONE]\n\n") + chain.flush()

    events = _events(out)
    texts = [
        event["delta"]["text"]
        for event in events
        if event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta"
    ]
    assert texts[0] == '{"'
    assert "".join(texts) == '{"sufficient": true, "questions": [], "dependencies": []}'
    starts = [event for event in events if event["type"] == "content_block_start"]
    assert starts == [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    assert events[-1]["type"] == "message_stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["default", "unmatched", "observe", "translate"])
async def test_dispatch_uses_translator_only_for_matching_opt_in(
    route: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SELFHOSTED_ROUTING_CONFIG", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-gateway-token")
    monkeypatch.setenv("LITELLM_PROXY_BASE_URL", BASE_URL)
    start = AsyncMock(return_value="http://127.0.0.1:12345")
    monkeypatch.setattr(SelfHostedShim, "start", start)
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-gateway-token"
        assert json.loads(request.content)["model"] == MODEL
        if route == "translate":
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "ready"},
                            "finish_reason": "stop",
                        },
                    ],
                },
            )
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ready"}]})

    settings = Settings()
    assert settings.SELFHOSTED_ROUTING_CONFIG == ""
    if route != "default":
        routing_file = tmp_path / "routing.yaml"
        target = _target("observe" if route == "observe" else "translate")
        if route == "translate":
            # Exercise the copyable issue-223 route, not a second hand-written config.
            example = yaml.safe_load(
                (Path(__file__).parents[2] / "routing.example.yaml").read_text(),
            )
            entry = next(
                row
                for row in example["selfhosted_routing"]
                if row["backend"] == "claude_code" and row["model"] == MODEL
            )
            target = entry["target"]
        routing_file.write_text(
            yaml.safe_dump(
                {
                    "selfhosted_routing": [
                        {
                            "backend": "claude_code",
                            "model": "another-model" if route == "unmatched" else MODEL,
                            "target": target,
                        },
                    ],
                },
            ),
        )
        monkeypatch.setenv("SELFHOSTED_ROUTING_CONFIG", str(routing_file))
        settings = Settings()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:

        async def probe(target: TargetDescriptor, **kwargs: Any) -> Any:
            return await check_health(target, **{**kwargs, "client": client})

        monkeypatch.setattr(launch, "check_health", probe)
        shim = await _launch_dispatch_proxy(
            Score(title="Assess", repo_url="https://example.com/owner/repo", branch="main"),
            "claude_code",
            MODEL,
            Stand(path=tmp_path, branch="main"),
            settings,
        )
    try:
        if route in {"default", "unmatched"}:
            assert shim is None
            start.assert_not_awaited()
            assert requests == []
            assert os.environ["ANTHROPIC_BASE_URL"] == BASE_URL
            assert os.environ["LITELLM_PROXY_BASE_URL"] == BASE_URL
        else:
            assert isinstance(shim, SelfHostedShim)
            assert shim.target.strategy == route
            assert shim._translate is (route == "translate")
            assert len(requests) == 1
            assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
            assert "LITELLM_PROXY_BASE_URL" not in os.environ
            if route == "translate":
                assert shim._upstream_url("/v1/messages") == BASE_URL + "/chat/completions"
                raw = shim._translate_request_bytes(
                    json.dumps(
                        {
                            "model": MODEL,
                            "stream": True,
                            "max_tokens": 512,
                            "messages": [{"role": "user", "content": "Assess"}],
                        },
                    ).encode(),
                )
                assert json.loads(raw)["model"] == MODEL
                assert json.loads(raw)["stream"] is True
                headers = shim._forward_headers({"x-api-key": "stale-cli-key"})
                assert headers == {"Authorization": "Bearer test-gateway-token"}
    finally:
        if shim is not None:
            await shim.stop()
    assert os.environ["ANTHROPIC_BASE_URL"] == BASE_URL
    assert os.environ["LITELLM_PROXY_BASE_URL"] == BASE_URL
