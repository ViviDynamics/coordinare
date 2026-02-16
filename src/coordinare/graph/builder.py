from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.nodes.monitor_agent import monitor_agent
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.nodes.notify import notify
from coordinare.graph.nodes.relay_feedback import relay_feedback
from coordinare.graph.routing import (
    route_from_agent_status,
    route_from_board_check,
    route_from_review,
)
from coordinare.graph.state import CoordinareState


async def _placeholder_node(state: CoordinareState) -> CoordinareState:
    return state


_DEFAULT_NODES: dict[str, Any] = {
    "check_board": check_board,
    "assess_card": assess_card,
    "dispatch_card": dispatch_card,
    "monitor_agent": monitor_agent,
    "monitor_pr": monitor_pr,
    "relay_feedback": relay_feedback,
    "merge_pr": merge_pr,
    "handle_blocked": handle_blocked,
    "notify": notify,
}


class CoordinareGraphBuilder:
    """Builds and compiles the orchestrator graph."""

    def __init__(
        self,
        checkpointer: Any = None,
        node_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._nodes = {**_DEFAULT_NODES, **(node_overrides or {})}

    def _node(self, name: str) -> Any:
        return self._nodes.get(name, _placeholder_node)

    def build(self) -> Any:
        graph = StateGraph(CoordinareState)

        graph.add_node("check_board", cast("Any", self._node("check_board")))
        graph.add_node("assess_card", cast("Any", self._node("assess_card")))
        graph.add_node("dispatch_card", cast("Any", self._node("dispatch_card")))
        graph.add_node("monitor_agent", cast("Any", self._node("monitor_agent")))
        graph.add_node("monitor_pr", cast("Any", self._node("monitor_pr")))
        graph.add_node("relay_feedback", cast("Any", self._node("relay_feedback")))
        graph.add_node("merge_pr", cast("Any", self._node("merge_pr")))
        graph.add_node("handle_blocked", cast("Any", self._node("handle_blocked")))
        graph.add_node("notify", cast("Any", self._node("notify")))

        graph.add_edge(START, "check_board")

        graph.add_conditional_edges(
            "check_board",
            route_from_board_check,
            {
                "dispatch": "assess_card",
                "monitor_pr": "monitor_pr",
                "monitor_agent": "monitor_agent",
                "idle": END,
            },
        )

        graph.add_edge("assess_card", "dispatch_card")
        graph.add_edge("dispatch_card", "notify")
        graph.add_edge("notify", END)

        graph.add_conditional_edges(
            "monitor_agent",
            route_from_agent_status,
            {
                "review": "monitor_pr",
                "blocked": "handle_blocked",
                "monitor": END,
            },
        )

        graph.add_conditional_edges(
            "monitor_pr",
            route_from_review,
            {
                "merge": "merge_pr",
                "relay": "relay_feedback",
                "blocked": "handle_blocked",
                "monitor": END,
            },
        )

        graph.add_edge("relay_feedback", END)
        graph.add_edge("merge_pr", "notify")
        graph.add_edge("handle_blocked", "notify")

        return graph.compile(checkpointer=self._checkpointer)
