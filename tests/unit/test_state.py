from __future__ import annotations

from datetime import datetime

from coordinare.graph.state import CoordinareState, initial_state


def test_initial_state_defaults() -> None:
    state = initial_state()
    assert state["current_card"] is None
    assert state["board_snapshot"] == {}
    assert state["phase"] == "idle"
    assert state["pending_reviews"] == []
    assert isinstance(state["last_poll_at"], datetime)
    assert state["error_count"] == 0
    assert state["github_field_cache"] == {}


def test_state_update_pattern() -> None:
    state = initial_state()
    state["phase"] = "dispatching"
    state["error_count"] += 1
    state["board_snapshot"] = {"TODO": ["PVI_1"]}

    assert state["phase"] == "dispatching"
    assert state["error_count"] == 1
    assert state["board_snapshot"]["TODO"] == ["PVI_1"]


def test_service_handles_declared_in_state_schema() -> None:
    """Regression: ``env_cache_service`` and ``performer_services_by_id`` must
    be declared on ``CoordinareState`` so LangGraph preserves them across
    ``graph.ainvoke()`` cycles. Without these declarations the state-merge
    silently drops the keys after the first cycle, and the dashboard's
    env-bootstrap endpoint 503s with "Env cache service not available" /
    "Bootstrap performer ... is not registered"."""
    annotations = CoordinareState.__annotations__
    assert "env_cache_service" in annotations, (
        "env_cache_service missing from CoordinareState — LangGraph will drop "
        "it after the first graph.ainvoke() cycle and the env-bootstrap "
        "dashboard endpoint will 503"
    )
    assert "performer_services_by_id" in annotations, (
        "performer_services_by_id missing from CoordinareState — LangGraph "
        "will drop it after the first graph.ainvoke() cycle and the "
        "env-bootstrap dashboard endpoint will 503"
    )


def test_initial_state_seeds_service_handles() -> None:
    """The seeded handles must be present (and falsy) so dashboard
    preconditions see the keys before the daemon's ``_run`` populates them."""
    state = initial_state()
    assert "env_cache_service" in state
    assert state["env_cache_service"] is None
    assert "performer_services_by_id" in state
    assert state["performer_services_by_id"] == {}


def test_service_handles_survive_langgraph_invocation() -> None:
    """End-to-end regression: a real ``StateGraph(CoordinareState)`` cycle must
    preserve ``env_cache_service`` and ``performer_services_by_id``. If either
    key is removed from ``CoordinareState.__annotations__``, LangGraph silently
    drops it during state merge and this test fails."""
    import asyncio

    from langgraph.graph import END, START, StateGraph

    sentinel_service = object()
    sentinel_registry = {"codex-ephemeral": object()}

    def passthrough(state: CoordinareState) -> dict:
        return {}

    graph = StateGraph(CoordinareState)
    graph.add_node("noop", passthrough)
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    compiled = graph.compile()

    state = initial_state()
    state["env_cache_service"] = sentinel_service  # type: ignore[typeddict-item]
    state["performer_services_by_id"] = sentinel_registry

    result = asyncio.run(compiled.ainvoke(state))

    assert result.get("env_cache_service") is sentinel_service, (
        "LangGraph dropped env_cache_service across the graph cycle"
    )
    assert result.get("performer_services_by_id") == sentinel_registry, (
        "LangGraph dropped performer_services_by_id across the graph cycle"
    )


# ---------------------------------------------------------------------------
# 066 FR-010: _retire_active_session contract
# ---------------------------------------------------------------------------


def test_retire_active_session_idempotent_on_fresh_state() -> None:
    """No active session → true no-op (no spurious active_sessions dict)."""
    from coordinare.graph.state import _retire_active_session

    state = initial_state()
    assert state.get("active_sessions") in (None, {})
    assert state.get("active_card_id") is None
    assert state.get("current_card") is None

    _retire_active_session(state)

    # Must not have materialised an active_sessions dict.
    assert state.get("active_sessions") in (None, {})
    assert state.get("active_card_id") is None
    assert state.get("current_card") is None


def test_retire_active_session_removes_session_and_clears_mirror() -> None:
    from coordinare.graph.state import _retire_active_session

    card = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state = initial_state()
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": {"current_card": card}}
    state["current_card"] = card

    _retire_active_session(state)

    assert state["active_card_id"] is None
    assert "ITEM_1" not in state["active_sessions"]
    assert state["current_card"] is None


def test_retire_active_session_noop_when_active_sessions_is_none() -> None:
    """Snapshot rehydration can surface ``active_sessions=None`` (vs ``{}``).
    Retire must early-return without materialising an empty dict."""
    from coordinare.graph.state import _retire_active_session

    state = initial_state()
    state["active_sessions"] = None  # type: ignore[typeddict-item]
    state["active_card_id"] = None
    state["current_card"] = None

    _retire_active_session(state)

    assert state["active_sessions"] is None
    assert state["active_card_id"] is None
    assert state["current_card"] is None


def test_retire_active_session_preserves_sibling_sessions() -> None:
    """Retiring one card must leave other sessions intact."""
    from coordinare.graph.state import _retire_active_session

    card_a = {"id": "CARD_A", "status": "IN_PROGRESS"}
    card_b = {"id": "CARD_B", "status": "IN_PROGRESS"}
    state = initial_state()
    state["active_card_id"] = "CARD_A"
    state["active_sessions"] = {
        "CARD_A": {"current_card": card_a},
        "CARD_B": {"current_card": card_b},
    }
    state["current_card"] = card_a

    _retire_active_session(state)

    assert "CARD_A" not in state["active_sessions"]
    assert state["active_sessions"]["CARD_B"]["current_card"] == card_b
    assert state["active_card_id"] is None
    assert state["current_card"] is None  # rederive sees active_card_id=None
