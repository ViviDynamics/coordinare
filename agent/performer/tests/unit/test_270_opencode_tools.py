"""OpenCode tool inputs and outputs carry paired lifecycle metadata."""
from __future__ import annotations

import pytest

from performer.backends.opencode import OpenCodeAdapter
from performer.backends.opencode_compat import OpenCodeCompatAdapter

CALL_ID = "call-install"
PART_ID = "part-install"


@pytest.mark.parametrize("adapter_type", [OpenCodeAdapter, OpenCodeCompatAdapter])
@pytest.mark.parametrize("separator", ["-", "_"])
@pytest.mark.parametrize("identity", [{"callID": CALL_ID, "id": PART_ID}, {"id": PART_ID}, {}])
def test_opencode_output_is_a_result_with_matching_call_id(adapter_type, separator, identity) -> None:
    adapter = adapter_type()
    for phase, data in (("input", {"command": "uv sync"}), ("output", "installed")):
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {
                "type": f"tool{separator}{phase}", "toolName": "bash", phase: data, **identity,
            }},
        })

    start, result = [event.model_dump(mode="json") for event in adapter.drain_events()]
    assert start["type"] == result["type"] == "tool_use"
    assert start["status"] == ""
    assert result["status"] == "ok"
    expected_id = identity.get("callID", identity.get("id", ""))
    assert start.get("call_id") == result.get("call_id") == expected_id
