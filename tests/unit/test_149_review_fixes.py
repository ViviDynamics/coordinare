"""#149 review fixes: the recorded To Do and Blocked waits must match the conductor.

Each test drives the real code path it names: the symphony cycle, the store's
snapshot build, and check_board with its pickup. The waits are read back from
the state the dashboard reads.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _compute_session_eligibilities
from coordinare.dashboard import DashboardStore
from coordinare.graph.nodes import check_board as check_board_module
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import SymphonyRuntimeState, initial_state
from tests.unit.test_dashboard import _make_mock_health, _make_mock_metrics


def _running(cid: str, board_status: str = "IN_PROGRESS") -> dict:
    return {
        "current_card": {"id": cid, "status": board_status, "issue_number": 9},
        "phase": "monitoring_performer",
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": f"job-{cid}"},
        "pipeline_admitted": True,
    }


def _idle_in_progress(cid: str) -> dict:
    """An IN_PROGRESS card whose session is idle: the pickup still counts it."""
    return {
        "current_card": {"id": cid, "status": "IN_PROGRESS", "issue_number": 9},
        "phase": "idle",
        "performer_stage": "implementing",
    }


def _parked(cid: str) -> dict:
    return {
        "current_card": {"id": cid, "status": "IN_REVIEW", "pr_number": 95},
        "phase": "monitoring_pr",
        "performer_stage": "closing_review",
        "pipeline_admitted": True,
    }


# --- item 1: session reasons reach the snapshot in symphony mode ------------ #


@pytest.mark.asyncio
async def test_a_queued_session_card_reaches_the_snapshot_in_symphony_mode(monkeypatch) -> None:
    daemon = CoordinareDaemon(AsyncMock())
    sym_state = SymphonyRuntimeState(name="sampleweb")
    sym_state.active_sessions = {
        "run": _running("run"),
        "queued": {
            "current_card": {"id": "queued", "status": "TODO", "issue_number": 65},
            "phase": "dispatching",
            "performer_stage": "assessing",
        },
    }
    daemon._state["symphony_states"] = {"sampleweb": sym_state}
    daemon._state["symphony_configs"] = {"sampleweb": SimpleNamespace(github_project_number=None,
                                                                    env_bootstrap_performer_id=None)}
    daemon._state["config"] = SimpleNamespace(
        max_concurrent_cards=1, max_cards_in_review=None, github_org="o",
        github_project_number=1, project_name="p",
    )

    async def real_gate(*_args, **_kwargs):
        # The graph's own gate, run with the symphony's sessions as the cycle sees them.
        _compute_session_eligibilities(daemon._state, daemon._state["active_sessions"], 1)

    monkeypatch.setattr(daemon, "_invoke_multi_session", real_gate)
    await daemon._conduct_single_symphony("sampleweb", SimpleNamespace())

    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    (entry,) = snapshot["symphonies"]
    assert entry["state"]["session_skip_reasons"]["queued"]["reason"] == "pipeline_capacity"
    assert entry["state"]["session_skip_reasons"]["queued"]["capacity"] == {"in_use": 1, "limit": 1}


@pytest.mark.asyncio
async def test_the_cycles_to_do_waits_reach_the_snapshot_in_symphony_mode(monkeypatch) -> None:
    """Second review: the cycle restored the shared keys and then mirrored the
    restored values, so a symphony never kept what its own cycle recorded."""
    daemon = CoordinareDaemon(AsyncMock())
    sym_state = SymphonyRuntimeState(name="sampleweb")
    daemon._state["symphony_states"] = {"sampleweb": sym_state}
    daemon._state["symphony_configs"] = {"sampleweb": SimpleNamespace(github_project_number=None,
                                                                    env_bootstrap_performer_id=None)}
    recorded = {"WEB": {"reason": "queued", "blockers": [], "detail": None}}
    capacity = {"in_use": 1, "limit": 1, "hold": "queued"}

    async def cycle_records_waits(*_args, **_kwargs):
        daemon._state["todo_waits"] = dict(recorded)
        daemon._state["todo_capacity"] = dict(capacity)

    monkeypatch.setattr(daemon, "_invoke_multi_session", cycle_records_waits)
    await daemon._conduct_single_symphony("sampleweb", SimpleNamespace())

    assert sym_state.todo_waits == recorded
    assert sym_state.todo_capacity == capacity
    # The shared keys are back to what the cycle found, so the next board starts clean.
    assert daemon._state.get("todo_waits") == {}
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    (entry,) = snapshot["symphonies"]
    assert entry["state"]["todo_waits"] == recorded
    assert entry["state"]["todo_capacity"] == capacity


# --- item 2: the To Do verdict is the pickup's own outcome ------------------ #


def _board(columns: dict, issue_numbers: dict | None = None, descriptions: dict | None = None) -> dict:
    ids = [cid for column in columns.values() for cid in column]
    numbers = issue_numbers or {}
    return {
        "snapshot": columns,
        "titles": {cid: f"Card {cid}" for cid in ids},
        "descriptions": descriptions or {},
        "issue_numbers": {cid: numbers.get(cid, 100 + i) for i, cid in enumerate(ids)},
        "issue_urls": {cid: f"https://github.com/o/p/issues/{100 + i}" for i, cid in enumerate(ids)},
        "content_node_ids": {cid: f"node_{cid}" for cid in ids},
        "item_labels": {},
        "item_field_values": {},
        "item_assignees": {},
        "pr_urls": {},
    }


def _state_for(board: dict, sessions: dict, *, limit: int = 1, review_limit: int | None = None) -> dict:
    state = initial_state()
    github = AsyncMock()
    github.poll_board.return_value = board
    github._org = "o"
    github._project_name = "p"
    github.get_file_blob_sha = AsyncMock(return_value=None)
    state["github_service"] = github
    state["config"] = SimpleNamespace(
        max_concurrent_cards=limit, max_cards_in_review=review_limit,
        github_org="o", project_name="p", assignee_filter=None, include_unassigned=True,
    )
    state["active_sessions"] = sessions
    return state


async def _pickup(board: dict, sessions: dict, **kwargs) -> tuple[dict, set[str]]:
    """Run the real check_board; return the state and the cards it picked up."""
    before = set(sessions)
    state = await check_board(_state_for(board, sessions, **kwargs))
    picked = {cid for cid in (state.get("active_sessions") or {}) if cid not in before}
    return state, picked


@pytest.mark.asyncio
async def test_slots_full_by_a_running_card_queues_the_todo_card() -> None:
    board = _board({"IN_PROGRESS": ["run"], "TODO": ["todo"]})
    state, picked = await _pickup(board, {"run": _running("run")})
    assert "todo" not in picked
    assert state["todo_waits"]["todo"]["reason"] == "queued"
    assert state["todo_capacity"]["in_use"] == 1 and state["todo_capacity"]["limit"] == 1


@pytest.mark.asyncio
async def test_an_idle_in_progress_card_holds_a_slot_for_pickup_and_the_verdict_agrees() -> None:
    """Pickup counts an IN_PROGRESS card whatever its phase, so the To Do card waits."""
    board = _board({"IN_PROGRESS": ["idle"], "TODO": ["todo"]})
    state, picked = await _pickup(board, {"idle": _idle_in_progress("idle")})
    assert "todo" not in picked
    assert state["todo_waits"]["todo"]["reason"] == "queued"
    assert state["todo_capacity"]["in_use"] == 1


@pytest.mark.asyncio
async def test_a_parked_review_card_leaves_the_slot_to_pickup() -> None:
    """Pickup does not count a card parked in review, so the To Do card is admitted."""
    board = _board({"IN_REVIEW": ["parked"], "TODO": ["todo"]})
    state, picked = await _pickup(board, {"parked": _parked("parked")}, review_limit=1)
    assert "todo" in picked
    assert "todo" not in state["todo_waits"]


@pytest.mark.asyncio
async def test_a_free_slot_records_no_reason() -> None:
    board = _board({"TODO": ["todo"]})
    state, picked = await _pickup(board, {})
    assert "todo" in picked
    assert "todo" not in state["todo_waits"]


@pytest.mark.asyncio
async def test_a_pending_dependency_is_named_with_its_blocker() -> None:
    board = _board(
        {"IN_PROGRESS": ["blocker"], "TODO": ["todo"]},
        issue_numbers={"blocker": 42},
        descriptions={"todo": "Depends on #42"},
    )
    state, picked = await _pickup(board, {})
    assert "todo" not in picked
    assert state["todo_waits"]["todo"] == {"reason": "waiting_on", "blockers": ["#42"], "verdict": "dependency"}


# --- item 3: a failed board read does not leave last cycle's verdicts ------- #


@pytest.mark.asyncio
async def test_a_failed_board_read_clears_the_previous_verdicts() -> None:
    board = _board({"TODO": ["todo"]})
    state = _state_for(board, {})
    state["github_service"].poll_board.side_effect = RuntimeError("board unreadable")
    state["todo_waits"] = {"todo": {"reason": "queued", "blockers": [], "detail": None}}
    state["todo_capacity"] = {"in_use": 1, "limit": 1, "waiting_on_review": 0, "review_limit": None, "hold": "queued"}
    result = await check_board(state)
    assert result["todo_waits"] == {}
    assert result["todo_capacity"] is None


# --- item 4: a symphony never inherits another symphony's verdicts ---------- #


@pytest.mark.asyncio
async def test_a_symphony_whose_first_check_fails_does_not_inherit_the_previous_verdicts(monkeypatch) -> None:
    daemon = CoordinareDaemon(AsyncMock())
    a, b = SymphonyRuntimeState(name="a"), SymphonyRuntimeState(name="b")
    a.todo_waits = {"from_a": {"reason": "queued", "blockers": [], "detail": None}}
    a.todo_capacity = {"in_use": 1, "limit": 1, "hold": "queued"}
    daemon._state["symphony_states"] = {"a": a, "b": b}
    daemon._state["symphony_configs"] = {
        name: SimpleNamespace(github_project_number=None, env_bootstrap_performer_id=None) for name in ("a", "b")
    }
    # The previous symphony's verdicts are still in the shared state when b starts.
    daemon._state["todo_waits"] = dict(a.todo_waits)
    daemon._state["todo_capacity"] = dict(a.todo_capacity)
    seen: list[tuple[dict, object]] = []

    async def first_check_of_b(*_args, **_kwargs):
        seen.append((dict(daemon._state.get("todo_waits") or {}), daemon._state.get("todo_capacity")))
        raise RuntimeError("first check failed")

    monkeypatch.setattr(daemon, "_invoke_multi_session", first_check_of_b)
    await daemon._conduct_single_symphony("b", SimpleNamespace())
    assert seen == [({}, None)]
    # The cycle restores what the symphony found on entry, not the other board's verdicts.
    assert daemon._state.get("todo_capacity") == dict(a.todo_capacity)


@pytest.mark.asyncio
async def test_a_failed_cycle_restores_the_verdicts_it_started_with(monkeypatch) -> None:
    daemon = CoordinareDaemon(AsyncMock())
    a = SymphonyRuntimeState(name="a")
    daemon._state["symphony_states"] = {"a": a}
    daemon._state["symphony_configs"] = {"a": SimpleNamespace(github_project_number=None, env_bootstrap_performer_id=None)}
    before = {"prev": {"reason": "queued", "blockers": [], "detail": None}}
    daemon._state["todo_waits"] = dict(before)

    async def mid_cycle_failure(*_args, **_kwargs):
        daemon._state["todo_waits"] = {"during": {"reason": "queued", "blockers": [], "detail": None}}
        raise RuntimeError("cycle failed")

    monkeypatch.setattr(daemon, "_invoke_multi_session", mid_cycle_failure)
    await daemon._conduct_single_symphony("a", SimpleNamespace())
    assert daemon._state["todo_waits"] == before


# --- item 5: a bug in the display path never aborts a cycle ----------------- #


@pytest.mark.asyncio
async def test_a_failing_wait_recorder_does_not_abort_check_board(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise ValueError("recorder bug")

    monkeypatch.setattr(check_board_module, "refresh_todo_waits", broken)
    board = _board({"TODO": ["todo"]})
    result = await check_board(_state_for(board, {}))
    # The cycle still picks the card up; the broken recorder only loses its display.
    assert result["todo_waits"] == {}
    assert "todo" in result.get("active_sessions", {})


@pytest.mark.asyncio
async def test_a_failing_dependency_recorder_does_not_abort_check_board(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise ValueError("recorder bug")

    monkeypatch.setattr(check_board_module, "record_dependency_waits", broken)
    board = _board({"TODO": ["todo"]})
    result = await check_board(_state_for(board, {}))
    assert "todo" in result.get("active_sessions", {})


# --- item 6: an unresolvable dependency is a known wait ---------------------- #


@pytest.mark.asyncio
async def test_a_card_blocked_by_an_off_board_dependency_says_what_it_waits_on() -> None:
    board = _board({"TODO": ["todo"]}, descriptions={"todo": "Depends on #77"})
    state = await check_board(_state_for(board, {}))
    assert state["todo_waits"]["todo"] == {"reason": "waiting_on", "blockers": ["#77"], "verdict": "dependency"}


@pytest.mark.asyncio
async def test_that_wait_survives_the_card_moving_to_blocked() -> None:
    board = _board({"TODO": ["todo"]}, descriptions={"todo": "Depends on #77"})
    state = await check_board(_state_for(board, {}))
    moved = _board({"BLOCKED": ["todo"]}, descriptions={"todo": "Depends on #77"})
    state["github_service"].poll_board.return_value = moved
    state = await check_board(state)
    assert state["todo_waits"]["todo"] == {"reason": "waiting_on", "blockers": ["#77"], "verdict": "dependency"}

