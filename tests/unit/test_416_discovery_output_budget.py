"""416: discovery and review get an output budget of their own, and a cut-off reply says so.

Discovery asks the conducting model for one large JSON reply. At the 4096-token
conducting default that reply was cut off and reported as malformed JSON.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coordinare.config import ConductingConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.services.conducting import OpenAiApiBackend, discovery_chat
from coordinare.services.conductor_calls import (
    ConductorCallLog,
    ModelOutputTruncatedError,
    ModelRequestFailedError,
    conductor_call,
)
from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    EnvironmentDiscoveryError,
    discover_and_review,
    discovery_output_tokens,
)
from coordinare.services.run_transcript import TranscriptCollector

COMMIT = "a" * 40
PATH = "README.md"


def _client(content: str, finish_reason: str | None = "stop") -> MagicMock:
    """An ``httpx.AsyncClient`` stand-in that answers every post with one chat completion."""
    choice: dict = {"message": {"content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    response = MagicMock(status_code=200)
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"choices": [choice]})
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=response)
    return client


def _call_log(root) -> ConductorCallLog:
    return ConductorCallLog(TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6, retention_days=0))


def _transcript_result(root) -> dict:
    (path,) = list(root.rglob("*.jsonl"))
    return json.loads(path.read_text().splitlines()[-1])


def _sent_budget(client: MagicMock) -> int:
    payload = client.post.call_args.kwargs["json"]
    return payload.get("max_completion_tokens", payload.get("max_tokens"))


async def _discover(chat):
    async def fetch(path):
        return "zog --version must report 7" if path == PATH else None
    return await discover_and_review(symphony_name="suite", commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat)


class TestBudget:
    def test_sized_for_the_largest_reply_discovery_admits(self):
        # About four bytes of JSON per token, held to the 64000-token output cap
        # the widest range of current models accepts.
        assert discovery_output_tokens(DiscoveryLimits()) == 64_000
        assert discovery_output_tokens(DiscoveryLimits(max_output_bytes=4096)) == 1024

    def test_the_short_prompt_conducting_default_is_unchanged(self):
        assert ConductingConfig().max_tokens == 4096
        assert ConductingConfig().discovery_max_tokens is None

    @pytest.mark.asyncio
    async def test_a_default_conducting_config_sends_the_discovery_budget(self):
        chat = discovery_chat(ConductingConfig(backend="openai_api", model="m"), api_key="k")
        client = _client("{}")
        with patch("httpx.AsyncClient", return_value=client):
            await chat([{"role": "user", "content": "x"}])
        assert _sent_budget(client) == 64_000

    @pytest.mark.asyncio
    async def test_an_operator_can_set_the_discovery_budget(self):
        cc = ConductingConfig(backend="openai_api", model="m", effort=None, discovery_max_tokens=16_384)
        chat = discovery_chat(cc, api_key="k")
        client = _client("{}")
        with patch("httpx.AsyncClient", return_value=client):
            await chat([{"role": "user", "content": "x"}])
        assert client.post.call_args.kwargs["json"]["max_tokens"] == 16_384

    @pytest.mark.parametrize("backend", ["anthropic_api", "claude_cli", "codex_cli", "opencode", "none"])
    def test_backends_without_the_json_chat_seam_have_no_discovery_chat(self, backend):
        assert discovery_chat(ConductingConfig(backend=backend), api_key="k") is None

    def test_the_daemon_builds_discovery_with_the_discovery_budget(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        cfg = SimpleNamespace(conducting=ConductingConfig(backend="openai_api", model="m"))
        holder = SimpleNamespace(_state={"config": cfg, "conductor_calls": None})
        chat = CoordinareDaemon._get_manifest_llm_chat(holder)
        assert chat.__self__._max_tokens == 64_000


class TestTruncation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
    async def test_a_cut_off_reply_raises_truncation(self, finish_reason):
        backend = OpenAiApiBackend(api_key="k", model="m", max_tokens=4096)
        with patch("httpx.AsyncClient", return_value=_client('{"manifest": {"sch', finish_reason)), \
                pytest.raises(ModelOutputTruncatedError) as caught:
            await backend.chat_json_checked([{"role": "user", "content": "x"}])
        assert caught.value.max_tokens == 4096

    @pytest.mark.asyncio
    async def test_the_finish_reason_is_on_the_reply(self):
        backend = OpenAiApiBackend(api_key="k", model="m")
        with patch("httpx.AsyncClient", return_value=_client("{}", "length")):
            reply = await backend._post_chat([{"role": "user", "content": "x"}], json_mode=True)
        assert reply.finish_reason == "length"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("finish_reason", ["stop", None])
    async def test_a_complete_reply_is_returned(self, finish_reason):
        backend = OpenAiApiBackend(api_key="k", model="m")
        with patch("httpx.AsyncClient", return_value=_client('{"ok": true}', finish_reason)):
            assert await backend.chat_json_checked([{"role": "user", "content": "x"}]) == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_other_json_prompts_keep_returning_text(self):
        backend = OpenAiApiBackend(api_key="k", model="m")
        with patch("httpx.AsyncClient", return_value=_client('{"a"', "length")):
            assert await backend.chat_json([{"role": "user", "content": "x"}]) == '{"a"'

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("content", "finish_reason", "error"), [
        ("", "stop", "empty reply"),
        ("", "content_filter", "finish_reason content_filter"),
    ])
    async def test_a_reply_with_no_usable_text_raises(self, content, finish_reason, error):
        backend = OpenAiApiBackend(api_key="k", model="m")
        with patch("httpx.AsyncClient", return_value=_client(content, finish_reason)), \
                pytest.raises(ModelRequestFailedError) as caught:
            await backend.chat_json_checked([{"role": "user", "content": "x"}])
        assert caught.value.error == error

    @pytest.mark.asyncio
    async def test_a_refused_request_names_its_http_status(self):
        request = httpx.Request("POST", "https://api.example/v1/chat/completions")
        refused = httpx.HTTPStatusError("bad request", request=request, response=httpx.Response(400, request=request))
        client = _client("{}")
        client.post.return_value.status_code = 400
        client.post.return_value.raise_for_status = MagicMock(side_effect=refused)
        backend = OpenAiApiBackend(api_key="k", model="m", max_tokens=64_000)
        with patch("httpx.AsyncClient", return_value=client), pytest.raises(ModelRequestFailedError) as caught:
            await backend.chat_json_checked([{"role": "user", "content": "x"}])
        assert caught.value.error == "http_400"
        assert caught.value.max_tokens == 64_000

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("content", "finish_reason", "recorded", "raised"), [
        ('{"manifest": {"sch', "length", "finish_reason length: output truncated at 4096 tokens",
         ModelOutputTruncatedError),
        ("", "stop", "empty reply", ModelRequestFailedError),
        ("", "content_filter", "finish_reason content_filter", ModelRequestFailedError),
    ])
    async def test_the_conductor_call_records_the_failure(
        self, tmp_path, content, finish_reason, recorded, raised,
    ):
        # The transcript must not show a held discovery as a successful call (Copilot on #420).
        log = _call_log(tmp_path)
        backend = OpenAiApiBackend(api_key="k", model="m", max_tokens=4096, calls=log)
        with patch("httpx.AsyncClient", return_value=_client(content, finish_reason)), \
                pytest.raises(raised):
            async with conductor_call(backend, kind="env_discovery") as call:
                await backend.chat_json_checked([{"role": "user", "content": "x"}])
        await log.drain()
        assert call.error == recorded
        assert call.reply is not None
        assert call.reply.text == content
        assert call.reply.finish_reason == finish_reason
        result = _transcript_result(tmp_path)
        assert result["type"] == "result"
        assert result["is_error"] is True

    @pytest.mark.asyncio
    async def test_a_complete_reply_is_not_recorded_as_an_error(self, tmp_path):
        log = _call_log(tmp_path)
        backend = OpenAiApiBackend(api_key="k", model="m", calls=log)
        with patch("httpx.AsyncClient", return_value=_client("{}", "stop")):
            async with conductor_call(backend, kind="env_discovery") as call:
                await backend.chat_json_checked([{"role": "user", "content": "x"}])
        await log.drain()
        assert call.error is None
        assert _transcript_result(tmp_path)["is_error"] is False

    @pytest.mark.asyncio
    async def test_a_failed_request_raises_instead_of_returning_nothing(self):
        backend = OpenAiApiBackend(api_key=None, model="m")
        with pytest.raises(ModelRequestFailedError, match="missing api key"):
            await backend.chat_json_checked([{"role": "user", "content": "x"}])


class TestDiscoveryReports:
    @pytest.mark.asyncio
    async def test_a_truncated_discovery_reply_is_reported_as_truncation(self):
        chat = discovery_chat(ConductingConfig(backend="openai_api", model="m"), api_key="k")
        with patch("httpx.AsyncClient", return_value=_client('{"manifest": {"schema_version": 1, "requi', "length")), \
                pytest.raises(EnvironmentDiscoveryError) as caught:
            await _discover(chat)
        message = str(caught.value)
        assert message.startswith("discovery output was truncated at 64000 tokens")
        assert "discovery_max_tokens" in message
        assert "malformed" not in message

    @pytest.mark.asyncio
    async def test_any_chat_callable_can_report_truncation_with_its_budget(self):
        calls = 0

        async def chat(messages):
            nonlocal calls
            calls += 1
            raise ModelOutputTruncatedError(2048)

        with pytest.raises(EnvironmentDiscoveryError, match=r"^discovery output was truncated at 2048 tokens"):
            await _discover(chat)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_a_failed_model_request_is_not_reported_as_malformed_json(self):
        async def chat(messages):
            raise ModelRequestFailedError("ReadTimeout")

        with pytest.raises(EnvironmentDiscoveryError) as caught:
            await _discover(chat)
        assert str(caught.value) == "discovery model request failed: ReadTimeout"

    @pytest.mark.asyncio
    async def test_a_bad_request_suggests_the_budget_may_be_refused(self):
        # A model capped below the discovery budget (gpt-4o-mini: 16384) answers 400.
        async def chat(messages):
            raise ModelRequestFailedError("http_400", max_tokens=64_000)

        with pytest.raises(EnvironmentDiscoveryError) as caught:
            await _discover(chat)
        message = str(caught.value)
        assert message.startswith("discovery model request failed: http_400")
        assert "64000" in message
        assert "conducting.discovery_max_tokens" in message

    @pytest.mark.asyncio
    async def test_a_complete_reply_within_the_output_limit_is_still_decoded(self):
        # A reply under max_output_bytes with finish_reason stop reaches the decoder unchanged.
        chat = discovery_chat(ConductingConfig(backend="openai_api", model="m"), api_key="k")
        body = json.dumps({"tool": "execute"})
        with patch("httpx.AsyncClient", return_value=_client(body, "stop")), \
                pytest.raises(EnvironmentDiscoveryError, match="unsupported tool"):
            await _discover(chat)
