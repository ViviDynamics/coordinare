from __future__ import annotations

import json

import pytest

from performer.proxy.assembler import SseStreamWriter
from performer.proxy.llm_turn import LLMResponse, StreamDelta, ToolCall


@pytest.mark.parametrize("plan", ["plan", ""])
@pytest.mark.parametrize("exposure", ["thinking", "prepend_content", "drop"])
@pytest.mark.parametrize("tools", [(), (ToolCall("call-1", "read", {"path": "file"}),)])
def test_stream_declares_message_before_text_and_matches_final_output(plan, exposure, tools):
    writer = SseStreamWriter(wire_format="responses", expose_plan_as=exposure)
    writer.start(plan)
    writer.delta(StreamDelta(kind="text", text="hel"))
    writer.delta(StreamDelta(kind="text", text="lo"))
    writer.finish(LLMResponse(content="hello", reasoning=plan, tool_calls=tools))
    events = [json.loads(line[6:]) for block in writer.blocks for line in block.splitlines() if line.startswith("data: ")]
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    final = events[-1]["response"]["output"]
    message_index = next(i for i, item in enumerate(final) if item["type"] == "message")
    message_open = [e for e in events if e["type"] == "response.output_item.added" and e["item"]["type"] == "message"]
    assert len(message_open) == 1
    part_open = next(e for e in events if e["type"] == "response.content_part.added")
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert message_open[0]["sequence_number"] < part_open["sequence_number"] < deltas[0]["sequence_number"]
    assert "".join(e["delta"] for e in deltas) == final[message_index]["content"][0]["text"]
    text_events = [e for e in events if e["type"].startswith(("response.output_text.", "response.content_part."))]
    assert all(e["output_index"] == message_index for e in text_events)
    for event in events:
        if event["type"] == "response.output_item.done":
            assert event["item"] == final[event["output_index"]]
