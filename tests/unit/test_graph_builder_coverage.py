"""Coverage tests for coordinare/graph/builder.py — _placeholder_node."""
from __future__ import annotations

import pytest

from coordinare.graph.builder import _placeholder_node
from coordinare.graph.state import initial_state


@pytest.mark.asyncio
async def test_placeholder_node_returns_same_state_object() -> None:
    """_placeholder_node is a no-op that returns the identical state object."""
    state = initial_state()
    result = await _placeholder_node(state)
    assert result is state


@pytest.mark.asyncio
async def test_placeholder_node_does_not_mutate_state() -> None:
    """_placeholder_node must not modify any state fields."""
    state = initial_state()
    state["phase"] = "monitoring_agent"
    state["error_count"] = 7
    result = await _placeholder_node(state)
    assert result["phase"] == "monitoring_agent"
    assert result["error_count"] == 7
