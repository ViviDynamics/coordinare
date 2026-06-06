"""078 US1 — harmony_tool_calls normalizer (T011, written FIRST / must FAIL).

LiteLLM's streaming harmony->tool_calls transform for gpt-oss leaks the raw
``<|channel|>commentary to=functions.<tool> <|constrain|>json<|message|>{args}<|call|>``
text into the assistant ``content`` instead of emitting structured ``tool_calls``
(LiteLLM #13300/#17246). The ``harmony_tool_calls`` normalizer reassembles that
leaked text into a well-formed ``tool_calls`` entry on BOTH the non-streaming JSON
path and the streaming SSE path, leaking zero ``<|channel|>``/``<|call|>`` markers
and never emitting a half-parsed call (SC-001 / SC-002, FR-078-7, FR-078-3).
"""

from __future__ import annotations

import json

import pytest

from performer.proxy.normalizers.harmony import HarmonyToolCallsNormalizer
from tests.unit.proxy.fixtures import (
    HARMONY_CLEAN_JSON,
    HARMONY_EXPECTED_ARGS,
    HARMONY_EXPECTED_TOOL_NAME,
    HARMONY_LEAKED_JSON,
    HARMONY_LEAKED_SSE_CHUNKS,
)

_HARMONY_MARKERS = ("<|channel|>", "<|constrain|>", "<|message|>", "<|call|>")


@pytest.fixture()
def norm():
    return HarmonyToolCallsNormalizer()


def test_registry_key(norm):
    assert norm.key == "harmony_tool_calls"


# --- non-streaming JSON path --------------------------------------------- #


def test_json_leaked_harmony_becomes_structured_tool_calls(norm):
    out = norm.normalize_json(HARMONY_LEAKED_JSON)
    message = out["choices"][0]["message"]

    tool_calls = message.get("tool_calls")
    assert tool_calls, "leaked harmony content must be reassembled into tool_calls"
    fn = tool_calls[0]["function"]
    assert fn["name"] == HARMONY_EXPECTED_TOOL_NAME
    assert json.loads(fn["arguments"]) == json.loads(HARMONY_EXPECTED_ARGS)
    assert tool_calls[0]["type"] == "function"

    # no leaked markers survive anywhere in the assistant content
    content = message.get("content") or ""
    for marker in _HARMONY_MARKERS:
        assert marker not in content


def test_json_clean_response_passes_through_unchanged(norm):
    # fail-open: a response with no harmony markers is returned untouched.
    out = norm.normalize_json(HARMONY_CLEAN_JSON)
    assert out == HARMONY_CLEAN_JSON
    assert "tool_calls" not in out["choices"][0]["message"]


# --- streaming SSE path --------------------------------------------------- #


def test_sse_reassembles_tool_call_across_deltas(norm):
    flt = norm.sse_filter()
    emitted = bytearray()
    for chunk in HARMONY_LEAKED_SSE_CHUNKS:
        emitted.extend(flt.feed(chunk))
    emitted.extend(flt.flush())
    out = bytes(emitted).decode("utf-8")

    # zero leaked markers anywhere in the rewritten stream (SC-001)
    for marker in _HARMONY_MARKERS:
        assert marker not in out, f"leaked harmony marker {marker!r} in SSE output"

    # collect the streamed tool_calls deltas into a name + arguments accumulator
    name_parts: list[str] = []
    arg_parts: list[str] = []
    for frame in out.split("\n\n"):
        line = next(
            (ln for ln in frame.split("\n") if ln.startswith("data:")), None
        )
        if line is None:
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        delta = json.loads(payload)["choices"][0].get("delta", {})
        for tc in delta.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            if fn.get("name"):
                name_parts.append(fn["name"])
            if fn.get("arguments"):
                arg_parts.append(fn["arguments"])

    assert "".join(name_parts) == HARMONY_EXPECTED_TOOL_NAME
    # no half-parsed call: the streamed arguments reassemble to valid JSON (SC-002)
    assert json.loads("".join(arg_parts)) == json.loads(HARMONY_EXPECTED_ARGS)
