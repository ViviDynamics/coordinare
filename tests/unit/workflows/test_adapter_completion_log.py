"""T001b: a finished workflow leaves its timings in the performer log.

The step durations live on ``WorkflowMetrics`` in memory. Nothing durable saw
them until the adapter logged them, and the live measurement T001b asks for
reads the container's stderr, so the completion line is the measurement.
"""

from __future__ import annotations

import pytest
from performer.workflows.adapter import WorkflowAdapter

from tests.unit.workflows._fakelog import FakeLog


def _fake_log(monkeypatch) -> FakeLog:
    import performer.workflows.adapter as adapter_mod

    fake = FakeLog()
    monkeypatch.setattr(adapter_mod, "log", fake)
    return fake

class _Score:
    role = "qa"
    model = "m"


@pytest.mark.asyncio
async def test_completion_logs_metrics_for_measurement(monkeypatch):
    fake = _fake_log(monkeypatch)
    adapter = WorkflowAdapter("noop", toolkit_factory=lambda metrics, sink: object())
    adapter._metrics.step_durations_ms["plan"] = 12
    adapter._metrics.model_calls = 3

    await adapter.start(object(), _Score())
    await adapter._task

    done = [e for e in fake.entries if e["event"] == "workflow.completed"]
    assert len(done) == 1
    entry = done[0]
    assert entry["workflow"] == "noop"
    assert entry["step_durations_ms"] == {"plan": 12}
    assert entry["model_calls"] == 3
    assert isinstance(entry["total_ms"], int) and entry["total_ms"] >= 0
    assert adapter.get_status().state != "error"


class _Boom:
    name = "boom"

    async def run(self, stand, score, toolkit):
        raise RuntimeError("no")


@pytest.mark.asyncio
async def test_failure_logs_timings_but_never_completed(monkeypatch):
    fake = _fake_log(monkeypatch)
    adapter = WorkflowAdapter("noop", toolkit_factory=lambda metrics, sink: object())
    adapter._workflow = _Boom()

    await adapter.start(object(), _Score())
    await adapter._task

    events = [e["event"] for e in fake.entries]
    assert "workflow.failed" in events
    assert "workflow.completed" not in events
    failed = next(e for e in fake.entries if e["event"] == "workflow.failed")
    assert "total_ms" in failed
    assert failed["step_durations_ms"] == {}
    assert adapter.get_status().state == "error"


def test_model_read_timeout_outlasts_a_doubled_reasoning_budget():
    """The first live QA run died at 300 s: a doubled 6000-token budget on a
    reasoning model needs longer than that at the gateway's generation rate.
    Connect stays short so a dead gateway still fails fast."""
    from performer.workflows.adapter import _MODEL_CONNECT_TIMEOUT_S, _MODEL_READ_TIMEOUT_S

    assert _MODEL_READ_TIMEOUT_S >= 900
    assert _MODEL_CONNECT_TIMEOUT_S <= 60


def test_step_budget_is_capped_by_the_role_max_tokens():
    from performer.workflows.adapter import _effective_max_tokens

    assert _effective_max_tokens(16000, 12288) == 12288
    assert _effective_max_tokens(8000, 32768) == 8000
    assert _effective_max_tokens(8000, 0) == 8000
    assert _effective_max_tokens(8000, None) == 8000
    assert _effective_max_tokens(8000, "not a number") == 8000


@pytest.mark.asyncio
async def test_model_call_logs_timing_and_parses_the_response_once(monkeypatch):
    import performer.workflows.adapter as adapter_mod
    from performer.config import get_settings

    parses = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            parses["n"] += 1
            return {
                "choices": [{"message": {"content": "{}", "reasoning_content": "r"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 42},
            }

    sent = {}

    class _Client:
        def __init__(self, *a, **kw):
            sent["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, content=None):
            import json as _json

            sent["body"] = _json.loads(content)
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)  # adapter imports httpx inside the caller
    monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://gateway.test")
    get_settings.cache_clear()

    class _S:
        model = "m"
        max_tokens = 12288

    fake = _fake_log(monkeypatch)
    reply = await adapter_mod._model_caller(_S())("persona", [{"type": "text", "text": "hi"}], 16000)

    get_settings.cache_clear()
    assert reply.finish_reason == "stop" and reply.reasoning_content == "r"
    assert sent["body"]["max_tokens"] == 12288, "doubled budget must be capped by the role"
    assert parses["n"] == 1, "the response body is parsed once"
    entry = next(e for e in fake.entries if e["event"] == "workflow.model_call")
    assert entry["completion_tokens"] == 42 and entry["max_tokens"] == 12288
    assert isinstance(entry["elapsed_ms"], int)
