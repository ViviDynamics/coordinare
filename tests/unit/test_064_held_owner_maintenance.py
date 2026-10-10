"""Sanitized native reproduction: legacy held owners need board metadata and feedback."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.dashboard import DashboardStore
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.state_store import PersistedSession, WorkflowSnapshot


async def passthrough(state):
    return state


class Board:
    def __init__(self):
        self.columns = {"BACKLOG": ["story"], "BLOCKED": ["peer"]}
        self.moves = []
        self.comment_reads = []

    async def poll_board(self):
        return {
            "snapshot": deepcopy(self.columns),
            "titles": {"story": "Synthetic CI hold", "peer": "Synthetic error"},
            "descriptions": {},
            "issue_numbers": {"story": 1, "peer": 2},
            "issue_urls": {
                "story": "https://github.com/example/sample/issues/1",
                "peer": "https://github.com/example/sample/issues/2",
            },
        }

    async def move_card(self, card_id, column):
        self.moves.append((card_id, column))
        for ids in self.columns.values():
            if card_id in ids:
                ids.remove(card_id)
        self.columns.setdefault(column, []).append(card_id)

    async def issue_number_for_card(self, card_id):
        return 1 if card_id == "story" else 2

    async def get_issue_comments(self, number, since_id=None):
        self.comment_reads.append((number, since_id))
        if number == 1 and (since_id or 0) < 101:
            return [
                {
                    "id": 101,
                    "author": "human",
                    "body": "LGTM",
                    "created_at": "2026-10-10T20:00:00Z",
                },
            ]
        return []

    async def get_issue_details(self, *_args, **_kwargs):
        return {"title": "Synthetic CI hold", "body": ""}

    async def add_comment(self, *_args, **_kwargs):
        return None


def restore():
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="blocked",
        active_sessions={
            "story": PersistedSession(
                card_id="story",
                phase="blocked",
                performer_stage="implementing",
                last_issue_comment_id=100,
                processed_issue_comment_ids=[100],
                card_clarifications=[{"answer": "Keep the prior NO."}],
                pr_artefacts={
                    "pr_number": 7,
                    "pr_node_id": "PR_existing",
                    "head_after": "head-before",
                    "pushed_branch": "conductor/story",
                },
            ),
            "peer": PersistedSession(
                card_id="peer",
                phase="blocked",
                performer_stage="assessing",
                board_paused=True,
                board_pause_column="TODO",
                board_pause_resume_phase="system_error",
                system_error_count=1,
                card_clarifications=[{"answer": "Return an empty string."}],
            ),
        },
    )
    graph = CoordinareGraphBuilder(
        node_overrides={
            "route_issue_comments": route_issue_comments,
            "check_board": check_board,
            "classify_scope": passthrough,
            "notify": passthrough,
        },
    ).build()
    daemon = CoordinareDaemon(graph)
    daemon._restore_from_snapshot(WorkflowSnapshot.model_validate_json(snapshot.model_dump_json()))
    board = Board()
    daemon.state.update(
        github_service=board,
        config=SimpleNamespace(max_concurrent_cards=1),
        lifecycle_sequence=["assessing", "implementing", "reviewing"],
    )
    return daemon, board


@pytest.mark.asyncio
async def test_legacy_held_owners_get_labels_from_actual_board_maintenance():
    daemon, board = restore()
    before = deepcopy(daemon.state["active_sessions"])
    for _ in range(3):
        await daemon._invoke_multi_session()
    rounds = DashboardStore._serialize_clarifications(daemon, None, {})
    assert [(r["card_id"], r.get("card_number"), r.get("card_title")) for r in rounds] == [
        ("story", 1, "Synthetic CI hold"),
        ("peer", 2, "Synthetic error"),
    ]
    for cid, s in before.items():
        assert (
            daemon.state["active_sessions"][cid]["card_clarifications"] == s["card_clarifications"]
        )
    assert board.moves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["story", "peer", None])
async def test_backlog_ack_reaches_actual_owner_independent_of_focus(focus):
    daemon, board = restore()
    daemon.state["active_card_id"] = focus
    before = deepcopy(daemon.state["active_sessions"])
    for _ in range(3):
        await daemon._invoke_multi_session()
    owner = daemon.state["active_sessions"]["story"]
    assert owner["last_issue_comment_id"] == 101, "Backlog sibling feedback was never ingested"
    assert owner["processed_issue_comment_ids"] == {100, 101}
    assert owner["card_clarifications"] == before["story"]["card_clarifications"]
    assert (
        owner["current_card"]["pr_number"] == 7
        and owner["current_card"]["head_after"] == "head-before"
        and owner["current_card"]["pushed_branch"] == "conductor/story"
    )
    assert owner["phase"] == "blocked" and owner["agent_dispatch"] == {} and board.moves == []
    assert daemon.state["active_sessions"]["peer"]["board_paused"] is True


@pytest.mark.asyncio
async def test_held_comment_rotation_is_bounded_and_fair():
    daemon, board = restore()
    original = daemon.state["active_sessions"].pop("story")
    ids = [f"held-{index}" for index in range(7)]
    for cid in ids:
        session = deepcopy(original)
        session["current_card"]["id"] = cid
        daemon.state["active_sessions"][cid] = session
    board.columns["BACKLOG"] = ids
    served = []

    async def number_for_card(cid):
        served.append(cid)
        return 1

    board.issue_number_for_card = number_for_card
    daemon.state["active_card_id"] = "peer"
    for _ in range(3):
        start = len(served)
        await daemon._invoke_multi_session()
        assert 0 < len(served) - start <= 3
    assert set(served) == set(ids)
    assert board.moves == []
    assert "backlog_comment_poll_ids" not in daemon.state


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("large", "small"), ("small", "large")])
async def test_held_comment_rotation_is_fair_across_symphonies(order):
    daemon, _ = restore()
    original = daemon.state["active_sessions"]["story"]
    runtimes = {}
    boards = {}
    served = {name: [] for name in order}
    for name, count in (("large", 7), ("small", 3)):
        ids = [f"{name}-{index}" for index in range(count)]
        sessions = {}
        for cid in ids:
            owner = deepcopy(original)
            owner["current_card"]["id"] = cid
            sessions[cid] = owner
        runtimes[name] = SymphonyRuntimeState(
            name=name,
            active_sessions=sessions,
            previous_phase="blocked",
            board_snapshot={"BACKLOG": ids},
        )
        board = Board()
        board.columns = {"BACKLOG": ids}

        async def number_for_card(cid, symphony=name):
            served[symphony].append(cid)
            return 1

        board.issue_number_for_card = number_for_card
        boards[name] = board
    daemon.state.update(
        active_sessions={
            cid: owner
            for runtime in runtimes.values()
            for cid, owner in runtime.active_sessions.items()
        },
        active_card_id=None,
        current_card=None,
        phase="blocked",
        symphony_states=runtimes,
        symphony_github_services=boards,
    )
    for _ in range(3):
        for name in order:
            before = len(served[name])
            await daemon._conduct_single_symphony(name, SimpleNamespace(name=name))
            assert runtimes[name].error_count == 0 and runtimes[name].last_error is None
            assert len(served[name]) - before <= 3
            assert "backlog_comment_poll_ids" not in daemon.state
    for name, runtime in runtimes.items():
        assert set(served[name]) == set(runtime.active_sessions)
        assert boards[name].moves == []
        for owner in runtime.active_sessions.values():
            assert owner["last_issue_comment_id"] == 101
            assert owner["phase"] == "blocked" and owner["agent_dispatch"] == {}
            assert owner["card_clarifications"] == [{"answer": "Keep the prior NO."}]
            assert owner["current_card"]["pr_number"] == 7
            assert owner["current_card"]["head_after"] == "head-before"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_board", [None, {"BACKLOG": "story"}])
async def test_invalid_board_does_not_hydrate_or_poll_held_owners(bad_board):
    daemon, board = restore()
    before = deepcopy(daemon.state["active_sessions"])

    async def invalid_poll():
        return {
            "snapshot": bad_board,
            "titles": {"story": "Untrusted title"},
            "issue_numbers": {"story": 99},
        }

    board.poll_board = invalid_poll
    await daemon._invoke_multi_session()
    assert board.comment_reads == []
    for cid, session in before.items():
        assert daemon.state["active_sessions"][cid]["current_card"] == session["current_card"]
        assert (
            daemon.state["active_sessions"][cid]["card_clarifications"]
            == session["card_clarifications"]
        )
    assert "backlog_comment_poll_ids" not in daemon.state


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "remove"])
async def test_comment_await_cannot_write_to_a_changed_owner(change):
    daemon, board = restore()
    original = daemon.state["active_sessions"]["story"]
    replacement = deepcopy(original)
    replacement.update(last_issue_comment_id=900, pending_override={"action": "restart"})
    replacement["current_card"]["head_after"] = "replacement-head"

    async def comments(number, since_id=None):
        if number == 1:
            if change == "replace":
                daemon.state["active_sessions"]["story"] = replacement
            else:
                daemon.state["active_sessions"].pop("story", None)
            return [
                {
                    "id": 101,
                    "author": "human",
                    "body": "Please also preserve Unicode.",
                    "created_at": "2026-10-10T20:00:00Z",
                },
            ]
        return []

    board.get_issue_comments = comments
    await daemon._invoke_multi_session()
    if change == "replace":
        assert daemon.state["active_sessions"]["story"] is replacement
        assert replacement["last_issue_comment_id"] == 900
        assert replacement["current_card"]["head_after"] == "replacement-head"
        assert replacement["pending_override"] == {"action": "restart"}
    else:
        assert "story" not in daemon.state["active_sessions"]
    assert original["last_issue_comment_id"] == 100
    assert original["card_clarifications"] == [{"answer": "Keep the prior NO."}]
    assert board.moves == []
