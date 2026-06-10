"""084 US1 — OpenAI ``/v1/chat/completions`` streaming → Anthropic ``/v1/messages``
streaming response translation (T007).

TDD: written BEFORE ``proxy/translate/sse.py`` exists and must FAIL first.
Covers the FR-003 / FR-005 streaming contract from
``contracts/response-translation.md``:

* the Anthropic event sequence emitted from an OpenAI ``chat.completion.chunk``
  stream: ``message_start`` → ``content_block_start`` / ``content_block_delta``
  / ``content_block_stop`` → ``message_delta`` → ``message_stop``
* ``message_start`` is emitted exactly once (first chunk only)
* a run of ``delta.content`` text deltas becomes one text block:
  ``content_block_start(text)`` then ``content_block_delta(text_delta)`` …
* the terminal ``finish_reason`` is captured and surfaced on the closing
  ``message_delta.delta.stop_reason`` via the shared FR-005 map
* chunk-boundary safety: ``STUB_OPENAI_STREAM_SSE_CHUNKS`` deliberately splits a
  ``data: …\n\n`` frame across two network chunks ("documents the bu" /
  "ild steps."); the stateful ``\n\n`` buffer must reassemble it (FR-003) so the
  reconstructed text is ``"The README documents the build steps."``

The translator is a pure :class:`StatefulSSEFilter` subclass (no network, no
disk, no body logging — FR-011), driven directly via ``feed()`` / ``flush()``.
"""

from __future__ import annotations

import json

from performer.proxy.translate.sse import TranslatingSSEFilter

from .fixtures import STUB_OPENAI_STREAM_SSE_CHUNKS


def _drive(chunks: list[bytes]) -> bytes:
    """Feed each upstream chunk through a fresh filter, then flush, returning the
    concatenated Anthropic-wire output bytes."""
    f = TranslatingSSEFilter()
    out = bytearray()
    for chunk in chunks:
        out += f.feed(chunk)
    out += f.flush()
    return bytes(out)


def _frames(raw: bytes) -> list[str]:
    """Split an SSE byte stream into individual ``…\\n\\n`` frame strings."""
    text = raw.decode("utf-8")
    return [frame for frame in text.split("\n\n") if frame.strip()]


def _event_kinds(raw: bytes) -> list[str]:
    kinds = []
    for frame in _frames(raw):
        for line in frame.splitlines():
            if line.startswith("event: "):
                kinds.append(line[len("event: "):])
    return kinds


def _data_payloads(raw: bytes) -> list[dict]:
    out = []
    for frame in _frames(raw):
        for line in frame.splitlines():
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
    return out


# --- event sequence --------------------------------------------------------- #


def test_stream_opens_with_message_start():
    kinds = _event_kinds(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    assert kinds[0] == "message_start"


def test_stream_closes_with_message_stop():
    kinds = _event_kinds(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    assert kinds[-1] == "message_stop"


def test_message_start_emitted_exactly_once():
    kinds = _event_kinds(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    assert kinds.count("message_start") == 1


def test_full_anthropic_event_sequence_present_and_ordered():
    kinds = _event_kinds(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    # The canonical order for a single text block. Text deltas stream per
    # upstream content chunk (contracts/response-translation.md: content_block_delta
    # "repeats", "first of a run" opens the block, "continuation" appends), so the
    # two upstream content frames ("The README " / "documents the build steps.")
    # produce two content_block_delta events rather than one coalesced delta.
    assert kinds == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_content_block_start_is_a_text_block():
    payloads = _data_payloads(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    starts = [p for p in payloads if p.get("type") == "content_block_start"]
    assert starts and starts[0]["content_block"]["type"] == "text"
    assert starts[0]["index"] == 0


# --- chunk-boundary safety (FR-003) ----------------------------------------- #


def test_split_frame_reassembled_into_full_text():
    """The second content delta is split mid-frame across the chunk boundary
    ("documents the bu" / "ild steps."); the stateful buffer must reassemble it
    so the concatenated ``text_delta`` text is the full sentence."""
    payloads = _data_payloads(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    text = "".join(
        p["delta"]["text"]
        for p in payloads
        if p.get("type") == "content_block_delta"
        and p["delta"].get("type") == "text_delta"
    )
    assert text == "The README documents the build steps."


# --- terminal stop_reason (FR-005) ------------------------------------------ #


def test_finish_reason_stop_maps_to_end_turn_on_message_delta():
    payloads = _data_payloads(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    deltas = [p for p in payloads if p.get("type") == "message_delta"]
    assert deltas and deltas[-1]["delta"]["stop_reason"] == "end_turn"


def test_content_block_stop_precedes_message_delta():
    kinds = _event_kinds(_drive(STUB_OPENAI_STREAM_SSE_CHUNKS))
    assert kinds.index("content_block_stop") < kinds.index("message_delta")
