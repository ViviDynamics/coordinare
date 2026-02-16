from __future__ import annotations

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import initial_state


@pytest.mark.asyncio
async def test_builder_compiles_and_invokes_default_graph() -> None:
    graph = CoordinareGraphBuilder().build()

    state = await graph.ainvoke(initial_state())

    assert isinstance(state, dict)
    assert "phase" in state


@pytest.mark.asyncio
async def test_builder_uses_node_overrides() -> None:
    async def _override_node(state):
        updated = dict(state)
        updated["phase"] = "dispatching"
        return updated

    builder = CoordinareGraphBuilder(node_overrides={"check_board": _override_node})
    graph = builder.build()

    state = await graph.ainvoke(initial_state())

    assert "phase" in state
