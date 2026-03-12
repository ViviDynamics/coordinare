"""Integration tests for the card clarification / Q&A loop.

Covers the real-world failure scenarios encountered during development:

1. Insufficient card → questions asked → user answers → card dispatched
2. Multiple Q&A rounds accumulate before dispatch
3. handle_blocked re-queues to TODO when assessment has no further questions
4. check_board preserves clarifications for the same card coming back from TODO
5. check_board clears clarifications when a different card is picked up
6. github_token is included in the dispatch payload via WorkspaceInfo
7. Performer returning status="error" on dispatch posts the reason, not a generic message
8. Performer returning session_expired auto-requeues silently (no human input needed)
9. clarifications survive a snapshot save/restore cycle
10. assess_card auto-dispatches when answered rounds exist and assessment has no new questions
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.state import CoordinareState, initial_state
from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import StateStore, WorkflowSnapshot
from tests.utils.fake_notification import FakeNotificationService

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _GitHubRecorder:
    """GitHub stub that records move_card and add_comment calls."""

    def __init__(
        self,
        board: dict[str, list[str]] | None = None,
        issue_details: dict | None = None,
        comments: list[dict] | None = None,
    ) -> None:
        self._board = board or {}
        self._issue_details = issue_details or {}
        self._comments = comments or []
        self.moves: list[tuple[str, str]] = []
        self.comments_posted: list[tuple[str, str]] = []

    async def poll_board(self) -> dict:
        return {
            "snapshot": self._board,
            "titles": {k: "Card" for col in self._board.values() for k in col},
            "descriptions": {k: "Description" for col in self._board.values() for k in col},
            "issue_numbers": {k: 1 for col in self._board.values() for k in col},
            "issue_urls": {k: "https://github.com/org/repo/issues/1" for col in self._board.values() for k in col},
            "content_node_ids": {k: "ISSUE_1" for col in self._board.values() for k in col},
            "item_labels": {},
        }

    async def get_issue_details(self, issue_id: str) -> dict:
        return self._issue_details or {"id": issue_id, "title": "Card", "body": "Description"}

    async def move_card(self, item_id: str, status: str) -> None:
        self.moves.append((item_id, status))
        # Update the board to reflect the move
        for _col, items in self._board.items():
            if item_id in items:
                items.remove(item_id)
        target = status.upper().replace(" ", "_")
        self._board.setdefault(target, []).append(item_id)

    async def add_comment(self, subject_id: str, body: str) -> dict:
        self.comments_posted.append((subject_id, body))
        return {"id": "C1"}

    async def get_pr_reviews(self, pr_id: str) -> list:
        return []

    async def check_mergeability(self, pr_id: str) -> dict:
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str) -> dict:
        return {"merged": True}


class _Assessment:
    """Configurable assessment backend."""

    def __init__(self, sufficient: bool = True, questions: list[str] | None = None) -> None:
        self._sufficient = sufficient
        self._questions = questions or []
        self.calls: list[dict] = []

    async def assess(self, card: dict) -> dict:
        self.calls.append(card)
        return {
            "sufficient": self._sufficient,
            "questions": self._questions,
            "rationale": "test",
        }


class _Agent:
    """Configurable agent service stub."""

    def __init__(self, dispatch_status: str = "accepted", status_marker: str = "working") -> None:
        self._dispatch_status = dispatch_status
        self._status_marker = status_marker
        self.dispatched: list[dict] = []

    async def check_health(self) -> dict:
        return {"status": "healthy"}

    async def dispatch_card(self, card_context: dict, workspace_info: Any = None) -> dict:
        self.dispatched.append(card_context)
        if self._dispatch_status == "error":
            return {"status": "error", "reason": "validation failed: missing github_token"}
        return {"status": "accepted", "session_id": "sess_1"}

    async def check_status(self, session_id: str) -> dict:
        return {"status": self._status_marker, "questions": []}

    async def relay_feedback(self, review_payload: dict) -> dict:
        return {"status": "acknowledged"}


@dataclass
class _WorkspaceInfo:
    repo_url: str = "https://github.com/org/repo"
    branch: str = "coordinare/ITEM_1/card"
    path: Path | None = None
    github_token: str = "ghp_test_token"


class _WorkspaceManager:
    def __init__(self, token: str = "ghp_test_token") -> None:
        self._token = token

    async def prepare(self, card: dict) -> _WorkspaceInfo:
        return _WorkspaceInfo(github_token=self._token)

    async def teardown(self, path: Path) -> None:
        pass


def _base_state(**overrides: Any) -> CoordinareState:
    state = initial_state()
    state["notification_service"] = FakeNotificationService()
    state["blocked_reminder_hours"] = 24
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# 1. Insufficient card → questions posted → no dispatch yet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_card_posts_questions() -> None:
    """A card with no description gets blocked with targeted questions."""
    github = _GitHubRecorder(board={"TODO": ["ITEM_1"]})
    assessment = _Assessment(sufficient=False, questions=["What routes should this affect?", "What are the acceptance criteria?"])
    agent = _Agent()

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        workspace_manager=_WorkspaceManager(),
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    # Card should be BLOCKED, not dispatched
    assert result["phase"] == "blocked"
    assert agent.dispatched == []
    # Questions should appear in the comment
    assert len(github.comments_posted) == 1
    _subject, body = github.comments_posted[0]
    assert "What routes should this affect?" in body


# ---------------------------------------------------------------------------
# 2. User answers → re-assessment → dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_answer_triggers_dispatch() -> None:
    """After the user answers blocked questions, the card is re-assessed and dispatched."""
    github = _GitHubRecorder(board={"BLOCKED": ["ITEM_1"]})
    # Assessment says sufficient now (after user provided answers)
    assessment = _Assessment(sufficient=True, questions=[])
    agent = _Agent()

    notified_at = datetime.now(UTC) - timedelta(minutes=5)
    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        workspace_manager=_WorkspaceManager(),
        current_card={
            "id": "ITEM_1",
            "issue_id": "ISSUE_1",
            "title": "Card",
            "description": "Description",
            "status": "BLOCKED",
        },
        open_questions=["What routes should this affect?"],
        card_clarifications=[],
        last_blocked_notified_at=notified_at,
    )

    # Simulate a user comment posted after we sent the questions
    github._issue_details = {
        "id": "ISSUE_1",
        "title": "Card",
        "body": "Description",
        "comments": {
            "nodes": [
                {
                    "body": "It should affect all authenticated routes.",
                    "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "author": {"login": "user"},
                }
            ]
        },
    }

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    # Card should have been dispatched
    assert result["phase"] in {"monitoring_agent", "dispatching"}
    assert len(agent.dispatched) == 1
    # Clarification history should be stored
    clarifications = result.get("card_clarifications") or []
    assert any(
        "authenticated routes" in c.get("answer", "")
        for c in clarifications
    )


# ---------------------------------------------------------------------------
# 3. Multi-round Q&A accumulates before dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_round_qa_accumulates() -> None:
    """Each user answer appends to card_clarifications; dispatch only happens when sufficient."""
    card_id = "ITEM_1"
    issue_id = "ISSUE_1"

    # Round 1: assessment is insufficient
    github = _GitHubRecorder(board={"BLOCKED": [card_id]})
    assessment = _Assessment(sufficient=False, questions=["What routes?"])
    agent = _Agent()

    notified_at = datetime.now(UTC) - timedelta(minutes=5)
    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        workspace_manager=_WorkspaceManager(),
        current_card={"id": card_id, "issue_id": issue_id, "title": "Card", "description": "", "status": "BLOCKED"},
        open_questions=["What routes?"],
        card_clarifications=[],
        last_blocked_notified_at=notified_at,
    )
    github._issue_details = {
        "id": issue_id, "title": "Card", "body": "",
        "comments": {"nodes": [{"body": "All auth routes.", "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "author": {"login": "user"}}]},
    }

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)
    # Still blocked — assessment was insufficient after round 1
    assert result["phase"] == "blocked"
    assert len(result.get("card_clarifications", [])) == 1

    # Round 2: assessment is now sufficient
    assessment._sufficient = True
    assessment._questions = []
    result["last_blocked_notified_at"] = datetime.now(UTC) - timedelta(minutes=5)
    result["open_questions"] = ["What format?"]
    # Inject a second comment
    github._board = {"BLOCKED": [card_id]}
    github._issue_details = {
        "id": issue_id, "title": "Card", "body": "",
        "comments": {"nodes": [
            {"body": "All auth routes.", "createdAt": (datetime.now(UTC) - timedelta(minutes=3)).isoformat().replace("+00:00", "Z"), "author": {"login": "user"}},
            {"body": "Breadcrumb > SubPage format.", "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "author": {"login": "user"}},
        ]},
    }

    result2 = await graph.ainvoke(result)
    assert result2["phase"] in {"monitoring_agent", "dispatching"}
    assert len(agent.dispatched) == 1
    # Both clarification rounds should be in the dispatch payload
    dispatched_card = agent.dispatched[0]
    clarifications = dispatched_card.get("clarifications") or []
    assert len(clarifications) == 2


# ---------------------------------------------------------------------------
# 4. handle_blocked re-queues to TODO when no new questions after answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blocked_requeues_when_sufficient() -> None:
    """When assessment produces no new questions and user has answered, card moves to TODO."""
    github = _GitHubRecorder(board={"BLOCKED": ["ITEM_1"]})
    # Assessment says no further questions needed
    assessment = _Assessment(sufficient=True, questions=[])

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        current_card={"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "Card", "description": "desc", "status": "BLOCKED"},
        open_questions=[],
        card_clarifications=[
            {"questions": ["What routes?"], "answer": "All authenticated routes."},
            {"questions": ["Any edge cases?"], "answer": "No."},
        ],
    )

    await handle_blocked(state)

    # Card should have been moved to TODO, not left BLOCKED
    assert ("ITEM_1", "TODO") in github.moves
    assert state["phase"] == "idle"
    # No comment should have been posted
    assert github.comments_posted == []


# ---------------------------------------------------------------------------
# 5. check_board preserves clarifications for same card from TODO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_preserves_clarifications_same_card() -> None:
    """When the same card returns to TODO (re-queued after Q&A), clarifications are kept."""
    github = _GitHubRecorder(
        board={"TODO": ["ITEM_1"]},
        issue_details={"id": "ISSUE_1", "title": "Card", "body": "desc"},
    )
    existing_clarifications = [
        {"questions": ["What routes?"], "answer": "All auth routes."},
    ]
    state = _base_state(
        github_service=github,
        current_card={"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "Card", "description": "desc", "status": "BLOCKED"},
        card_clarifications=existing_clarifications,
    )

    result = await check_board(state)

    assert result.get("card_clarifications") == existing_clarifications


# ---------------------------------------------------------------------------
# 6. check_board clears clarifications for a different card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_clears_clarifications_different_card() -> None:
    """When a brand-new card is picked up from TODO, old clarifications are wiped."""
    github = _GitHubRecorder(
        board={"TODO": ["ITEM_2"]},
        issue_details={"id": "ISSUE_2", "title": "Other Card", "body": ""},
    )
    state = _base_state(
        github_service=github,
        current_card={"id": "ITEM_1", "title": "Old Card", "status": "DONE"},
        card_clarifications=[{"questions": ["Old question?"], "answer": "Old answer."}],
    )

    result = await check_board(state)

    assert result.get("card_clarifications") == []
    assert result["current_card"]["id"] == "ITEM_2"


# ---------------------------------------------------------------------------
# 7. github_token flows through WorkspaceInfo → dispatch payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_payload_includes_github_token() -> None:
    """WorkspaceInfo.github_token is forwarded to the performer in the dispatch payload."""
    github = _GitHubRecorder(board={"TODO": ["ITEM_1"]})
    assessment = _Assessment(sufficient=True, questions=[])

    received_payloads: list[dict] = []

    class _CapturingAgent(_Agent):
        async def dispatch_card(self, card_context, workspace_info=None):
            from unittest.mock import AsyncMock, MagicMock

            from coordinare.services.agent_service import AgentService

            # Re-build the payload exactly as AgentService would
            transport = MagicMock()
            response = MagicMock()
            response.model_dump.return_value = {"status": "accepted", "session_id": "s1"}
            transport.send = AsyncMock(return_value=response)

            svc = AgentService(transport)
            result = await svc.dispatch_card(card_context, workspace_info=workspace_info)
            if transport.send.called:
                received_payloads.append(transport.send.call_args[0][0].payload)
            return result

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=_CapturingAgent(),
        workspace_manager=_WorkspaceManager(token="ghp_secret_token"),
    )

    graph = CoordinareGraphBuilder().build()
    await graph.ainvoke(state)

    assert received_payloads, "dispatch_card was never called"
    payload = received_payloads[0]
    assert payload.get("github_token") == "ghp_secret_token"


# ---------------------------------------------------------------------------
# 8. Performer dispatch error → meaningful comment, not generic fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_error_enters_retry_cycle() -> None:
    """When the performer returns status=error, the coordinare enters the system_error
    retry cycle — it does NOT immediately block the card or post a GitHub comment.
    The first invocation records the error and waits 90s before retrying."""
    github = _GitHubRecorder(board={"TODO": ["ITEM_1"]})
    assessment = _Assessment(sufficient=True, questions=[])
    agent = _Agent(dispatch_status="error")

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        workspace_manager=_WorkspaceManager(),
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    # First error: enters retry wait (0s elapsed < 90s threshold) → goes idle
    assert result["phase"] == "idle"
    assert result["system_error_count"] == 1
    assert result.get("system_error_reason") is not None
    # No GitHub comment — system errors do not pollute the issue thread
    assert len(github.comments_posted) == 0
    # Card is NOT moved to BLOCKED yet
    assert not any(status == "BLOCKED" for _, status in github.moves)


# ---------------------------------------------------------------------------
# 9. Performer session_expired → meaningful message (not empty open_questions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_expired_auto_requeues_silently() -> None:
    """When the performer reports session_expired, the card is silently re-queued to TODO.

    No comment is posted — session expiry is a transient system failure,
    not something that requires human input.
    """
    github = _GitHubRecorder(board={"IN_PROGRESS": ["ITEM_1"]})
    assessment = _Assessment(sufficient=True, questions=[])
    agent = _Agent(status_marker="session_expired")

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        current_card={"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "Card", "description": "desc", "status": "IN_PROGRESS"},
        agent_dispatch={"session_id": "sess_old"},
        card_clarifications=[],
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    assert result["phase"] == "idle"
    assert result["open_questions"] == []
    assert result["agent_dispatch"] == {}
    # Card moved back to TODO for re-dispatch on the next cycle — no comment posted
    assert ("ITEM_1", "TODO") in github.moves
    assert len(github.comments_posted) == 0


# ---------------------------------------------------------------------------
# 10. clarifications survive snapshot save/restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarifications_survive_snapshot(tmp_path: Path) -> None:
    """card_clarifications and active_card_issue_id are persisted in the state file."""

    path = tmp_path / "state.json"
    metrics = CoordinareMetrics()
    store = StateStore(path=path, metrics=metrics)

    clarifications = [
        {"questions": ["What routes?"], "answer": "All authenticated routes."},
        {"questions": ["Any edge cases?"], "answer": "No."},
    ]
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="blocked",
        active_card_id="PVTI_abc",
        active_card_title="Breadcrumbs",
        active_card_column="BLOCKED",
        active_card_issue_id="I_xyz",
        card_clarifications=clarifications,
        open_questions=["Any edge cases?"],
    )
    await store.save(snapshot)

    # Reload
    store2 = StateStore(path=path, metrics=CoordinareMetrics())
    loaded = await store2.load()

    assert loaded is not None
    assert loaded.card_clarifications == clarifications
    assert loaded.active_card_issue_id == "I_xyz"
    assert loaded.open_questions == ["Any edge cases?"]


# ---------------------------------------------------------------------------
# 11. assess_card auto-dispatches when answered rounds + no new questions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_card_dispatches_after_answered_rounds() -> None:
    """assess_card proceeds to dispatch when assessment has nothing new to ask but history exists."""
    github = _GitHubRecorder(board={"TODO": ["ITEM_1"]})
    # Assessment returns insufficient with no questions — should be overridden to sufficient
    assessment = _Assessment(sufficient=False, questions=[])
    agent = _Agent()

    state = _base_state(
        github_service=github,
        assessment_backend=assessment,
        agent_service=agent,
        workspace_manager=_WorkspaceManager(),
        # Pre-set current_card so check_board recognises this is the same card
        # returning from a re-queue (not a fresh card) and preserves clarifications.
        current_card={"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "Card", "description": "desc", "status": "BLOCKED"},
        card_clarifications=[
            {"questions": ["What routes?"], "answer": "All authenticated routes."},
            {"questions": ["Edge cases?"], "answer": "None."},
        ],
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    assert result["phase"] in {"monitoring_agent", "dispatching"}
    assert len(agent.dispatched) == 1
    # Clarifications should be embedded in the dispatched card context
    dispatched = agent.dispatched[0]
    assert dispatched.get("clarifications") is not None
    assert len(dispatched["clarifications"]) == 2


# ---------------------------------------------------------------------------
# 12. Clarifications forwarded to performer in dispatch payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarifications_included_in_dispatch_payload() -> None:
    """Q&A history appears in the payload sent to the performer."""
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.services.agent_service import AgentService

    clarifications = [
        {"questions": ["What routes?"], "answer": "All auth routes."},
    ]

    transport = MagicMock()
    response = MagicMock()
    response.model_dump.return_value = {"status": "accepted", "session_id": "s1"}
    transport.send = AsyncMock(return_value=response)

    svc = AgentService(transport)
    workspace_info = _WorkspaceInfo(github_token="ghp_tok")
    card_context = {
        "title": "Breadcrumbs",
        "description": "Add breadcrumbs",
        "id": "ITEM_1",
        "clarifications": clarifications,
    }

    await svc.dispatch_card(card_context, workspace_info=workspace_info)

    payload = transport.send.call_args[0][0].payload
    assert "clarifications" in payload
    assert payload["clarifications"][0]["answer"] == "All auth routes."
    assert payload["github_token"] == "ghp_tok"
