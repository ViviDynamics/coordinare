"""An ineligible focused terminal story must retain the human Backlog hold."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.check_board import check_board
from coordinare.session import create_session_from_card, session_to_state
from coordinare.state_store import PersistedSession, WorkflowSnapshot


async def passthrough(state):
    return state


class SyntheticBoard:
    def __init__(self, peer, dependency=None):
        self.columns = {"BACKLOG": ["story"], **({"BLOCKED": ["peer"]} if peer else {})}
        self.moves = []
        self.dependency = dependency
        if dependency:
            self.columns.setdefault(dependency, []).append("dependency")

    async def poll_board(self):
        return {"snapshot": deepcopy(self.columns), "titles": {"story": "Synthetic CI hold", "peer": "Synthetic error"},
                "descriptions": {"story": "Depends on #2"} if self.dependency else {},
                "issue_numbers": {"story": 1, "dependency": 2}}

    async def move_card(self, card_id, column):
        self.moves.append((card_id, column))
        for ids in self.columns.values():
            if card_id in ids:
                ids.remove(card_id)
        self.columns.setdefault(column, []).append(card_id)

    async def issue_number_for_card(self, card_id):
        return 1 if card_id == "story" else 2

    async def get_issue_comments(self, *_args, **_kwargs):
        return []

    async def get_issue_details(self, *_args, **_kwargs):
        return {"title": "Synthetic CI hold", "body": ""}

    async def add_comment(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("peer", [False, True])
async def test_all_ineligible_fallback_keeps_backlog_terminal_owner(restore, peer):
    board = SyntheticBoard(peer)
    owner = create_session_from_card({"id": "story", "status": "IN_PROGRESS", "title": "Synthetic CI hold", "issue_id": "synthetic-story"})
    owner.update(phase="blocked", performer_stage="implementing", agent_dispatch={}, board_paused=False,
                 env_blocked={"cause": "missing_failure_evidence", "action": "inspect CI"},
                 last_blocked_notified_at=datetime.now(UTC), blueprint={"summary": "Retain the plan"},
                 card_clarifications=[{"answer": "Keep the existing human instruction"}],
                 relay_feedback=[{"message": "Retain the review request"}])
    original_history = owner["card_clarifications"]
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
    sessions = {"story": owner}
    if peer:
        sibling = create_session_from_card({"id": "peer", "status": "IN_PROGRESS", "issue_id": "synthetic-peer"})
        sibling.update(phase="system_error", performer_stage="assessing", agent_dispatch={}, board_paused=False,
                       system_error_reason="BACKEND_FORMAT_ERROR", system_error_count=3)
        sessions["peer"] = sibling
    graph = CoordinareGraphBuilder(node_overrides={"route_issue_comments": passthrough, "check_board": check_board,
                                                   "classify_scope": passthrough, "notify": passthrough}).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(active_sessions=sessions, active_card_id="story", github_service=board,
                        config=SimpleNamespace(max_concurrent_cards=1), lifecycle_sequence=["assessing", "implementing", "reviewing"])
    session_to_state(owner, daemon.state)
    for _ in range(3):
        await daemon._invoke_multi_session()
    assert board.columns["BACKLOG"] == ["story"]
    assert board.moves == []
    retained = daemon.state["active_sessions"]["story"]
    assert retained["blueprint"] == {"summary": "Retain the plan"}
    expected_history = [{"answer": "Keep the existing human instruction"}]
    if restore:
        expected_history[0].update(card_id="story", card_title="Synthetic CI hold", stage="implementing")
    assert retained["card_clarifications"] == expected_history
    assert original_history == [{"answer": "Keep the existing human instruction"}]
    assert retained["relay_feedback"] == [{"message": "Retain the review request"}]
    assert retained["performer_stage"] == "implementing"
    assert retained["agent_dispatch"] == {}
    assert not retained.get("session_graph_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_backlog_maintenance_admits_todo_sibling_without_resuming_held_work(limit):
    board = SyntheticBoard(False)
    board.columns["TODO"] = ["new-story"]
    owner = create_session_from_card({"id": "story", "status": "IN_PROGRESS", "issue_id": "synthetic-story"})
    owner.update(phase="blocked", performer_stage="implementing", agent_dispatch={},
                 env_blocked={"cause": "missing_failure_evidence"}, blueprint={"summary": "Retain the plan"})
    graph = CoordinareGraphBuilder(node_overrides={"route_issue_comments": passthrough, "check_board": check_board,
                                                   "classify_scope": passthrough, "notify": passthrough}).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(active_sessions={"story": owner}, active_card_id="story", github_service=board,
                        config=SimpleNamespace(max_concurrent_cards=limit), lifecycle_sequence=["assessing", "implementing"])
    session_to_state(owner, daemon.state)
    await daemon._invoke_multi_session()
    assert board.columns["BACKLOG"] == ["story"]
    assert "new-story" in daemon.state["active_sessions"]
    assert daemon.state["active_sessions"]["new-story"]["phase"] == "dispatching"
    assert daemon.state["active_sessions"]["story"]["blueprint"] == {"summary": "Retain the plan"}
    assert daemon.state["active_sessions"]["story"]["agent_dispatch"] == {}
    assert board.moves == []


@pytest.mark.asyncio
async def test_legitimate_blocked_focus_still_runs_its_handler():
    board = SyntheticBoard(False)
    board.columns = {"BLOCKED": ["story"]}
    owner = create_session_from_card({"id": "story", "status": "BLOCKED", "issue_id": "synthetic-story"})
    owner.update(phase="blocked", performer_stage="implementing", agent_dispatch={},
                 env_blocked={"cause": "missing_failure_evidence"}, last_blocked_notified_at=datetime.now(UTC))
    graph = CoordinareGraphBuilder(node_overrides={"route_issue_comments": passthrough, "check_board": check_board,
                                                   "classify_scope": passthrough, "notify": passthrough}).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(active_sessions={"story": owner}, active_card_id="story", github_service=board,
                        config=SimpleNamespace(max_concurrent_cards=1), lifecycle_sequence=["assessing", "implementing"])
    session_to_state(owner, daemon.state)
    await daemon._invoke_multi_session()
    assert board.columns == {"BLOCKED": ["story"]}
    assert board.moves == [("story", "BLOCKED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["TODO", "IN_PROGRESS", "BLOCKED", "DONE"])
@pytest.mark.parametrize("restore", [False, True])
async def test_backlog_hold_is_independent_of_dependency_skip_reason(dependency, restore):
    board = SyntheticBoard(False, dependency)
    owner = create_session_from_card({"id": "story", "status": "IN_PROGRESS", "issue_id": "synthetic-story"})
    owner.update(phase="blocked", performer_stage="implementing", agent_dispatch={}, board_paused=False,
                 env_blocked={"cause": "missing_failure_evidence"}, last_blocked_notified_at=datetime.now(UTC),
                 relay_feedback=[{"message": "Retain the review request"}])
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
    graph = CoordinareGraphBuilder(node_overrides={"route_issue_comments": passthrough, "check_board": check_board,
                                                   "classify_scope": passthrough, "notify": passthrough}).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(active_sessions={"story": owner}, active_card_id="story", github_service=board,
                        config=SimpleNamespace(max_concurrent_cards=1), lifecycle_sequence=["assessing", "implementing"])
    session_to_state(owner, daemon.state)
    for _ in range(3):
        await daemon._invoke_multi_session()
    assert board.columns["BACKLOG"] == ["story"]
    assert not any(card_id == "story" for card_id, _ in board.moves)
    assert daemon.state["active_sessions"]["story"]["relay_feedback"] == [{"message": "Retain the review request"}]
    assert daemon.state["active_sessions"]["story"]["agent_dispatch"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("peer", [False, True])
async def test_backlog_maintenance_routes_new_comments_without_running_held_work(restore, peer):
    from coordinare.graph.nodes.route_issue_comments import route_issue_comments

    owner = create_session_from_card({"id": "story", "status": "IN_PROGRESS", "issue_id": "synthetic-story"})
    owner.update(phase="blocked", performer_stage="implementing", agent_dispatch={}, board_paused=False,
                 env_blocked={"cause": "missing_failure_evidence"}, last_blocked_notified_at=datetime.now(UTC),
                 blueprint={"summary": "Retain plan"}, card_clarifications=[{"answer": "Existing answer"}],
                 relay_feedback=[{"message": "Existing review"}], last_issue_comment_id=10,
                 processed_issue_comment_ids={10})
    owner["current_card"].update(pr_number=7, head_after="existing-head", pushed_branch="conductor/story")
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
    sessions = {"story": owner}
    if peer:
        sibling = create_session_from_card({"id": "peer", "status": "BLOCKED", "issue_id": "synthetic-peer"})
        sibling.update(phase="system_error", performer_stage="assessing", agent_dispatch={}, board_paused=False)
        sessions["peer"] = sibling
    control = {"control_id": "arrived-during-comments", "action": "restart"}

    class CommentBoard(SyntheticBoard):
        def __init__(self):
            super().__init__(peer)
            self.reads = []

        async def get_issue_comments(self, issue_number, since_id=None):
            assert issue_number == 1
            self.reads.append(("story", since_id))
            sessions["story"]["pending_override"] = deepcopy(control)
            return [{"id": identifier, "author": "human", "body": body, "created_at": "2026-10-10T00:00:00Z"}
                    for identifier, body in [(11, "Return an empty string for blank input."),
                                             (12, "Please also add a Unicode example."), (13, "LGTM"), (14, "")]
                    if since_id is None or identifier > since_id]

    board = CommentBoard()
    graph = CoordinareGraphBuilder(node_overrides={"route_issue_comments": route_issue_comments, "check_board": check_board,
                                                   "classify_scope": passthrough, "notify": passthrough}).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(active_sessions=sessions, active_card_id="story", github_service=board,
                        config=SimpleNamespace(max_concurrent_cards=1), lifecycle_sequence=["assessing", "implementing"])
    session_to_state(owner, daemon.state)
    for _ in range(3):
        await daemon._invoke_multi_session()
    retained = daemon.state["active_sessions"]["story"]
    assert [entry.get("comment_id") for entry in retained["card_clarifications"]] == [None, 11, 12]
    assert retained["requirements_changed"] is True
    assert retained["last_issue_comment_id"] == 14
    assert retained["processed_issue_comment_ids"] == {10, 11, 12, 13, 14}
    assert board.reads == [("story", 10), ("story", 14), ("story", 14)]
    assert retained["pending_override"] == control
    assert {key: retained["current_card"][key] for key in ("pr_number", "head_after", "pushed_branch")} == {
        "pr_number": 7, "head_after": "existing-head", "pushed_branch": "conductor/story"}
    assert retained["blueprint"] == {"summary": "Retain plan"}
    assert retained["relay_feedback"] == [{"message": "Existing review"}]
    assert retained["phase"] == "blocked"
    assert retained["performer_stage"] == "implementing"
    assert retained["agent_dispatch"] == {}
    assert board.columns["BACKLOG"] == ["story"]
    assert board.moves == []
    if peer:
        assert daemon.state["active_sessions"]["peer"]["card_clarifications"] == []
