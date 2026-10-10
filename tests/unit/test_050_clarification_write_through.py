from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.dashboard import DashboardStore, is_session_stale
from coordinare.graph.nodes.check_board import _collect_blocked_clarification
from coordinare.graph.nodes.dispatch_performer import _base_card_context
from coordinare.services.board_provider import MoveOutcome
from coordinare.services.conducting import _build_assess_prompt
from coordinare.services.pipeline_budget import select_pipelines
from coordinare.session import create_session_from_card, session_to_state
from coordinare.state_store import PersistedSession, WorkflowSnapshot

ANSWER = "Proceed only with the tests-only correction; do not repair or merge."
QUESTION = "May I continue?"


class AnswerBoard:
    def __init__(self, paused_column="BACKLOG", *, fresh=True, author="alice"):
        self.columns = {"paused": paused_column, "question": "BLOCKED"}
        self.answer_at = datetime.now(UTC) + timedelta(seconds=1 if fresh else -120)
        self.author = author

    async def poll_board(self):
        snapshot = {}
        for cid, column in self.columns.items():
            snapshot.setdefault(column, []).append(cid)
        return {
            "snapshot": snapshot,
            "titles": {"paused": "Paused story", "question": "Question story"},
            "issue_numbers": {"paused": 1, "question": 2},
            "content_node_ids": {"paused": "I_paused", "question": "I_question"},
        }

    async def get_card(self, card_id):
        assert card_id == "I_question"
        return {"comments": {"nodes": [{
            "id": "human-answer", "author": {"login": self.author},
            "createdAt": self.answer_at.isoformat(), "body": ANSWER,
        }]}}

    async def move_card(self, card_id, status):
        self.columns[card_id] = status
        return MoveOutcome(moved=True)

    async def get_card_comments(self, *_args, **_kwargs):
        return []


def blocked_question():
    session = create_session_from_card({
        "id": "question", "status": "BLOCKED", "pr_node_id": "existing-pr",
        "pushed_branch": "conductor/existing", "head_after": "existing-head",
    })
    session.update(
        phase="blocked", performer_stage="assessing", open_questions=[QUESTION],
        last_blocked_notified_at=datetime.now(UTC) - timedelta(minutes=1),
        dispatched_feedback={"stage": "assessing", "items": [{"id": "fb-1", "body": "Keep this correction"}]},
        pending_override={"action": "restart", "target_stage": "assessing", "control_id": "pending-command"},
        agent_dispatch={"session_id": "previous-terminal-worker"},
        agent_dispatch_at=datetime.now(UTC) - timedelta(hours=2),
    )
    return session


def assert_answer_survives(session):
    assert session["phase"] == "dispatching"
    assert session["open_questions"] == []
    assert session["card_clarifications"] == [{"questions": [QUESTION], "answer": ANSWER}]
    assert session["last_blocked_notified_at"] is None
    assert session["agent_dispatch"] == {}
    assert session["agent_dispatch_at"] is None
    assert session["performer_stage"] == "assessing"
    assert session["dispatched_feedback"]["items"][0]["body"] == "Keep this correction"
    assert session["pending_override"]["control_id"] == "pending-command"
    assert session["current_card"]["pr_node_id"] == "existing-pr"
    assert session["current_card"]["pushed_branch"] == "conductor/existing"
    assert session["current_card"]["head_after"] == "existing-head"


@pytest.mark.asyncio
async def test_clarification_consumer_writes_answer_and_resume_state_to_owner():
    board = AnswerBoard()
    session = blocked_question()
    daemon = CoordinareDaemon(None)
    daemon.state.update(active_sessions={"question": session}, active_card_id="question")
    session_to_state(session, daemon.state)
    await _collect_blocked_clarification(daemon.state, board, {}, "question", {"question": "I_question"})
    assert board.columns["question"] == "IN_PROGRESS"
    assert_answer_survives(session)
    assert daemon.state["agent_dispatch_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["paused", "question", None])
@pytest.mark.parametrize("paused_column", ["BACKLOG", "BLOCKED"])
async def test_real_paused_maintenance_retains_answer_for_next_session_and_restart(focus, paused_column):
    board = AnswerBoard(paused_column)
    paused = create_session_from_card({"id": "paused", "status": paused_column})
    paused.update(phase="blocked", board_paused=True, board_pause_column="BACKLOG")
    session = blocked_question()
    daemon = CoordinareDaemon(None)
    daemon.state.update(
        config=SimpleNamespace(max_concurrent_cards=1),
        active_sessions={"paused": paused, "question": session}, active_card_id=focus,
        board_provider=board, github_service=board,
    )
    if focus:
        session_to_state(daemon.state["active_sessions"][focus], daemon.state)
    before = copy.deepcopy(paused)
    await daemon._invoke_multi_session()
    assert board.columns["question"] == "IN_PROGRESS"
    assert_answer_survives(daemon.state["active_sessions"]["question"])
    assert daemon.state["active_sessions"]["paused"] == before
    persisted = _persist_one_session("question", daemon.state["active_sessions"]["question"])
    restored = _restored_session_dict(
        "question", PersistedSession.model_validate_json(persisted.model_dump_json()),
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="dispatching"),
        session["current_card"],
    )
    assert_answer_survives(restored)
    summary = DashboardStore._session_summary(daemon, "question", restored)
    assert summary["session_id"] is None
    assert summary["agent_dispatch_at"] is None
    assert is_session_stale(summary["agent_dispatch_at"]) is False
    assert select_pipelines(daemon.state["active_sessions"], 1, {"question"}) == {"question"}
    resumed = dict(daemon.state)
    session_to_state(restored, resumed)
    context, _ = _base_card_context(resumed, resumed["current_card"], "question", "assessing")
    assert context["clarifications"] == [{"questions": [QUESTION], "answer": ANSWER}]
    prompt = _build_assess_prompt(context)
    assert QUESTION in prompt
    assert ANSWER in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh,author", [(False, "alice"), (True, "helper[bot]")])
async def test_old_or_bot_comment_does_not_resume_or_supply_human_answer(fresh, author):
    board = AnswerBoard(fresh=fresh, author=author)
    session = blocked_question()
    daemon = CoordinareDaemon(None)
    daemon.state.update(active_sessions={"question": session}, active_card_id="question")
    session_to_state(session, daemon.state)
    before = copy.deepcopy(session)
    result = await _collect_blocked_clarification(daemon.state, board, {}, "question", {"question": "I_question"})
    assert result is None
    assert session == before
    assert board.columns["question"] == "BLOCKED"
