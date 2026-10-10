"""Held issue feedback must survive the normal save gate and real disk restore."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.dispatch_performer import _base_card_context
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.session import create_session_from_card, session_to_state
from coordinare.state_store import StateStore


async def passthrough(state):
    return state


class HeldBoard:
    def __init__(self, body, peer):
        self.body = body
        self.deliver = False
        self.columns = {"BACKLOG": ["story"], **({"BLOCKED": ["peer"]} if peer else {})}
        self.moves = []
        self.reads = []

    async def poll_board(self):
        return {
            "snapshot": deepcopy(self.columns),
            "titles": {"story": "Synthetic held story", "peer": "Synthetic held peer"},
            "issue_numbers": {"story": 1, "peer": 2},
            "issue_urls": {"story": "https://github.com/example/sample/issues/1"},
        }

    async def move_card(self, card_id, column):
        self.moves.append((card_id, column))

    async def issue_number_for_card(self, card_id):
        return 1 if card_id == "story" else 2

    async def get_issue_comments(self, number, since_id=None):
        self.reads.append((number, since_id))
        if number == 1 and self.deliver and (since_id or 0) < 11:
            return [{
                "id": 11, "author": "synthetic-human", "body": self.body,
                "created_at": "2026-10-10T00:00:00Z",
            }]
        return []

    async def get_issue_details(self, *_args, **_kwargs):
        return {"title": "Synthetic held story", "body": ""}

    async def add_comment(self, *_args, **_kwargs):
        return None


def make_daemon(body, peer):
    board = HeldBoard(body, peer)
    owner = create_session_from_card({
        "id": "story", "status": "IN_PROGRESS", "title": "Synthetic held story",
        "issue_number": 1, "issue_id": "I_story",
        "issue_url": "https://github.com/example/sample/issues/1",
        "pr_number": 7, "head_after": "existing-head", "pushed_branch": "conductor/story",
    })
    owner.update(
        phase="blocked", performer_stage="implementing", agent_dispatch={},
        last_issue_comment_id=10, processed_issue_comment_ids={10},
        card_clarifications=[{"answer": "Keep the existing human NO."}],
        blueprint={"summary": "Keep the plan"}, relay_feedback=[{"message": "Keep the review"}],
    )
    sessions = {"story": owner}
    if peer:
        sibling = create_session_from_card({"id": "peer", "status": "BLOCKED"})
        sibling.update(
            phase="blocked", performer_stage="assessing", board_paused=True,
            board_pause_column="TODO", board_pause_resume_phase="system_error",
            system_error_count=1,
        )
        sessions["peer"] = sibling
    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": route_issue_comments, "check_board": check_board,
        "classify_scope": passthrough, "notify": passthrough,
    }).build()
    daemon = CoordinareDaemon(graph)
    daemon.state.update(
        active_sessions=sessions, active_card_id="story", github_service=board,
        config=SimpleNamespace(max_concurrent_cards=1),
        lifecycle_sequence=["assessing", "implementing", "reviewing"],
    )
    session_to_state(owner, daemon.state)
    return daemon, board


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", [False, True])
@pytest.mark.parametrize("body,label", [
    ("LGTM", "approval"), ("", "noise"), ("Still blocked on a human decision.", "blocker_update"),
    ("Please preserve Unicode.", "clarification"), ("Please also add a Unicode example.", "scope_change"),
])
async def test_held_comment_intake_survives_native_save_gate_and_disk_restart(tmp_path, peer, body, label):
    daemon, board = make_daemon(body, peer)
    metrics = MagicMock()
    store = StateStore(tmp_path / "synthetic.state.json", metrics)
    daemon._state_store = store
    await daemon._invoke_multi_session()
    await store.save(daemon._build_snapshot())
    previous = daemon._lifecycle_signature()
    before = deepcopy(daemon.state["active_sessions"])
    # Unchanged held polling must stay quiet on disk.
    await daemon._invoke_multi_session()
    assert await daemon._save_snapshot_if_changed(previous) == previous
    assert metrics.state_last_written_timestamp.set.call_count == 1
    board.deliver = True
    await daemon._invoke_multi_session()
    live = daemon.state["active_sessions"]["story"]
    assert live["last_issue_comment_id"] == 11 and live["processed_issue_comment_ids"] == {10, 11}
    assert live["phase"] == "blocked" and live["agent_dispatch"] == {} and board.moves == []
    assert live["card_clarifications"][0] == before["story"]["card_clarifications"][0]
    if peer:
        assert daemon.state["active_sessions"]["peer"] == before["peer"]
    history = deepcopy(live["card_clarifications"])
    latest = await daemon._save_snapshot_if_changed(previous)
    saved = await store.load()
    assert saved is not None
    cold, cold_board = make_daemon(body, peer)
    cold_board.deliver = True
    cold._restore_from_snapshot(saved)
    restored = cold.state["active_sessions"]["story"]
    assert restored["last_issue_comment_id"] == 11, "Normal save gate lost consumed issue comment"
    assert restored["processed_issue_comment_ids"] == {10, 11}
    assert restored["phase"] == "blocked" and restored["agent_dispatch"] == {}
    assert restored["blueprint"] == before["story"]["blueprint"]
    assert restored["relay_feedback"] == before["story"]["relay_feedback"]
    assert restored["current_card"]["pr_number"] == 7
    assert restored["current_card"]["head_after"] == "existing-head"
    assert restored["current_card"]["pushed_branch"] == "conductor/story"
    assert len(restored["card_clarifications"]) == len(history)
    for original, actual in zip(history, restored["card_clarifications"], strict=True):
        assert all(actual[k] == value for k, value in original.items())
        assert actual["card_id"] == "story" and actual["stage"] == "implementing"
    actionable = label in {"clarification", "scope_change"}
    assert len(history) == (2 if actionable else 1)
    if actionable:
        assert restored["card_clarifications"][1]["body"] == body
        assert restored["card_clarifications"][1]["classification"] == label
    assert restored["requirements_changed"] is (label == "scope_change")
    resumed = dict(cold.state)
    session_to_state(restored, resumed)
    context, _ = _base_card_context(resumed, restored["current_card"], "story", "implementing")
    assert context["clarifications"] == restored["card_clarifications"]
    if actionable:
        assert context["clarifications"][1]["body"] == body
    assert live["card_clarifications"] == history
    assert latest != previous and metrics.state_last_written_timestamp.set.call_count == 2
    # Repeated polling neither redispatches nor duplicates the received feedback or disk write.
    for _ in range(2):
        await daemon._invoke_multi_session()
        assert await daemon._save_snapshot_if_changed(latest) == latest
    assert metrics.state_last_written_timestamp.set.call_count == 2
    await cold._invoke_multi_session()
    assert cold_board.moves == [] and (1, 11) in cold_board.reads
    assert cold.state["active_sessions"]["story"]["processed_issue_comment_ids"] == {10, 11}
    assert len(cold.state["active_sessions"]["story"]["card_clarifications"]) == len(history)
