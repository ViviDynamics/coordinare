"""OpenCode tool inputs and outputs carry paired lifecycle metadata."""
from __future__ import annotations

from typing import Any

import pytest

from performer.backends.opencode import OpenCodeAdapter
from performer.backends.opencode_compat import OpenCodeCompatAdapter

CALL_ID = "call-install"
PART_ID = "part-install"


@pytest.mark.parametrize("adapter_type", [OpenCodeAdapter, OpenCodeCompatAdapter])
@pytest.mark.parametrize("separator", ["-", "_"])
@pytest.mark.parametrize("identity", [{"callID": CALL_ID, "id": PART_ID}, {"id": PART_ID}, {"callID": "", "id": PART_ID}, {}])
def test_opencode_output_is_a_result_with_matching_call_id(adapter_type, separator, identity) -> None:
    adapter = adapter_type()
    for phase, data in (("input", {"command": "uv sync"}), ("output", "installed")):
        adapter._handle_event({
            "type": "message.part.updated",
            "properties": {"part": {
                "type": f"tool{separator}{phase}", "toolName": "bash", phase: data,
                "state": {"status": "completed"} if phase == "output" else {"status": "running"}, **identity,
            }},
        })

    start, result = [event.model_dump(mode="json") for event in adapter.drain_events()]
    assert start["type"] == result["type"] == "tool_use"
    assert start["status"] == ""
    assert result["status"] == "ok"
    expected_id = identity.get("callID") or identity.get("id", "")
    assert start.get("call_id") == result.get("call_id") == expected_id


@pytest.mark.parametrize("adapter_type", [OpenCodeAdapter, OpenCodeCompatAdapter])
@pytest.mark.parametrize("separator", ["-", "_"])
@pytest.mark.parametrize("state,expected_status", [
    ({"status": "completed"}, "ok"),
    ({"status": "error"}, "error"),
    ({"status": "running"}, ""),
    ({}, ""),
    (None, ""),
    ("error", ""),
])
def test_opencode_output_status_comes_from_tool_state(adapter_type, separator, state, expected_status) -> None:
    part: dict[str, Any] = {"type": f"tool{separator}output", "toolName": "bash", "output": "install output"}
    if state is not None:
        part["state"] = state
    adapter = adapter_type()
    adapter._handle_event({"type": "message.part.updated", "properties": {"part": part}})

    [result] = adapter.drain_events()
    assert result.status == expected_status


@pytest.mark.parametrize("adapter_type", [OpenCodeAdapter, OpenCodeCompatAdapter])
def test_opencode_call_id_pairs_parts_with_different_part_ids(adapter_type) -> None:
    adapter = adapter_type()
    for phase in ("input", "output"):
        adapter._handle_event({
            "type": "message.part.updated", "properties": {"part": {
                "type": f"tool-{phase}", "toolName": "bash", "callID": CALL_ID,
                "id": f"{PART_ID}-{phase}", phase: "bundle install",
            }},
        })

    start, result = adapter.drain_events()
    assert start.call_id == result.call_id == CALL_ID
