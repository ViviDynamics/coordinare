from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from coordinare.graph.nodes.advocate import advocate_scan
from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
from coordinare.graph.nodes.classify_scope import classify_scope_node
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.nodes.handle_system_error import handle_system_error
from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.nodes.notify import notify
from coordinare.graph.nodes.relay_feedback import relay_feedback
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.routing import (
    route_from_agent_status,
    route_from_assess,
    route_from_board_check,
    route_from_dispatch,
    route_from_review,
    route_from_system_error,
)
from coordinare.graph.state import CoordinareState


async def _placeholder_node(state: CoordinareState) -> CoordinareState:
    return state


_DEFAULT_NODES: dict[str, Any] = {
    "advocate_scan": advocate_scan,
    "route_issue_comments": route_issue_comments,
    "check_board": check_board,
    "assess_card": assess_card,
    "classify_scope": classify_scope_node,
    "dispatch_card": dispatch_performer,
    "monitor_agent": monitor_performer,
    "monitor_pr": monitor_pr,
    "relay_feedback": relay_feedback,
    "classify_human_feedback": classify_human_feedback,
    "merge_pr": merge_pr,
    "handle_blocked": handle_blocked,
    "handle_system_error": handle_system_error,
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

        graph.add_node("advocate_scan", cast("Any", self._node("advocate_scan")))
        graph.add_node("route_issue_comments", cast("Any", self._node("route_issue_comments")))
        graph.add_node("check_board", cast("Any", self._node("check_board")))
        graph.add_node("assess_card", cast("Any", self._node("assess_card")))
        graph.add_node("classify_scope", cast("Any", self._node("classify_scope")))
        graph.add_node("dispatch_card", cast("Any", self._node("dispatch_card")))
        graph.add_node("monitor_agent", cast("Any", self._node("monitor_agent")))
        graph.add_node("monitor_pr", cast("Any", self._node("monitor_pr")))
        graph.add_node("relay_feedback", cast("Any", self._node("relay_feedback")))
        graph.add_node("classify_human_feedback", cast("Any", self._node("classify_human_feedback")))
        graph.add_node("merge_pr", cast("Any", self._node("merge_pr")))
        graph.add_node("handle_blocked", cast("Any", self._node("handle_blocked")))
        graph.add_node("handle_system_error", cast("Any", self._node("handle_system_error")))
        graph.add_node("notify", cast("Any", self._node("notify")))

        graph.add_edge(START, "advocate_scan")
        graph.add_edge("advocate_scan", "route_issue_comments")
        graph.add_edge("route_issue_comments", "check_board")
        # 074: classify_scope is a no-op when persona_scope.enabled is False
        # (default) — it sits between board pickup and the dispatcher so the
        # per-card PersonaScope is computed (eventually) before performers run.
        graph.add_edge("classify_scope", "dispatch_card")

        graph.add_conditional_edges(
            "check_board",
            route_from_board_check,
            {
                "assess": "assess_card",       # legacy: no assessor performer configured
                "dispatch": "classify_scope",  # classify per-persona scope, then dispatch
                "monitor_pr": "monitor_pr",
                "monitor_agent": "monitor_agent",
                "blocked": "handle_blocked",
                "handle_system_error": "handle_system_error",
                "idle": END,
            },
        )

        graph.add_conditional_edges(
            "assess_card",
            route_from_assess,
            {
                "dispatch": "classify_scope",
                "blocked": "handle_blocked",
                "monitor_pr": "monitor_pr",
            },
        )
        graph.add_conditional_edges(
            "dispatch_card",
            route_from_dispatch,
            {
                "blocked": "handle_blocked",
                "handle_system_error": "handle_system_error",
                "notify": "notify",
            },
        )
        graph.add_edge("notify", END)

        graph.add_conditional_edges(
            "monitor_agent",
            route_from_agent_status,
            {
                "review": "monitor_pr",
                "blocked": "handle_blocked",
                "handle_system_error": "handle_system_error",
                "dispatch": "classify_scope",
                "monitor": END,
            },
        )

        graph.add_conditional_edges(
            "handle_system_error",
            route_from_system_error,
            {"dispatch": "classify_scope", "idle": END},
        )

        graph.add_conditional_edges(
            "monitor_pr",
            route_from_review,
            {
                "merge": "merge_pr",
                "relay": "classify_human_feedback",
                "blocked": "handle_blocked",
                "monitor": END,
            },
        )

        # classify_human_feedback sets phase="dispatching" → next cycle picks
        # it up via check_board routing.  relay_feedback is kept as a legacy
        # node for any direct callers.
        graph.add_edge("classify_human_feedback", END)
        graph.add_edge("relay_feedback", END)
        graph.add_edge("merge_pr", "notify")
        graph.add_edge("handle_blocked", "notify")

        return graph.compile(checkpointer=self._checkpointer)
