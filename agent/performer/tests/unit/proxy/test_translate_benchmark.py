"""084 SC-006 — wire-translation overhead budget (T021).

Measures the in-process cost of the translate strategy EXCLUDING upstream/network
time: one inbound request translation (Anthropic → OpenAI) plus one outbound
response translation in BOTH modes — non-streaming JSON and the streaming SSE
filter driven over a full chunk sequence — for a qa-representative exchange
(system prompt + user turn, tools present, streaming). Asserts the per-request
overhead stays ≤ 5 ms median and ≤ 15 ms p99.

Deterministic and fully in-process: the "upstream" is the canned stub byte
stream, so the measured time is pure translator glue (no live network, no model).
"""

from __future__ import annotations

import time

from performer.proxy.translate.request import translate_request
from performer.proxy.translate.response import translate_response
from performer.proxy.translate.sse import TranslatingSSEFilter

from .fixtures import STUB_OPENAI_NONSTREAM_JSON, STUB_OPENAI_STREAM_SSE_CHUNKS

_MEDIAN_BUDGET_MS = 5.0
_P99_BUDGET_MS = 15.0


def _representative_request() -> dict:
    """A qa-representative Anthropic exchange: system + user turn, tools present."""
    return {
        "model": "gpt-oss:120b",
        "max_tokens": 1024,
        "stream": True,
        "system": "You are a coding agent. Edit files to satisfy the spec.",
        "messages": [
            {"role": "user", "content": "Read README.md and summarize the build steps."},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll read it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Run `make build` then `make test`.",
                    }
                ],
            },
        ],
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file from the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        "tool_choice": {"type": "auto"},
    }


def _one_request_overhead(body: dict) -> float:
    """Wall time (ms) for one full translate round trip, excluding any network."""
    t0 = time.perf_counter()
    translate_request(body)
    translate_response(dict(STUB_OPENAI_NONSTREAM_JSON))
    f = TranslatingSSEFilter()
    for chunk in STUB_OPENAI_STREAM_SSE_CHUNKS:
        f.feed(chunk)
    f.flush()
    return (time.perf_counter() - t0) * 1000.0


def test_translate_overhead_within_budget():
    body = _representative_request()
    _one_request_overhead(body)  # warm up import/JIT paths

    samples = sorted(_one_request_overhead(body) for _ in range(50))
    median_ms = samples[len(samples) // 2]
    p99_ms = samples[min(len(samples) - 1, int(len(samples) * 0.99))]

    assert median_ms <= _MEDIAN_BUDGET_MS, (
        f"translate median overhead {median_ms:.3f}ms exceeds {_MEDIAN_BUDGET_MS}ms"
    )
    assert p99_ms <= _P99_BUDGET_MS, (
        f"translate p99 overhead {p99_ms:.3f}ms exceeds {_P99_BUDGET_MS}ms"
    )
