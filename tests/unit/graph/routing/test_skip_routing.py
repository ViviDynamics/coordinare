"""074 T035 — Skip-routing test.

When the per-card ``PersonaScope`` marks a persona's depth as ``"skip"``,
``dispatch_performer`` must advance the lifecycle past that persona without
ever invoking its performer service (FR-008).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinare.config import (
    PersonaConfig,
    PersonasConfig,
    ScopeBehavior,
    ScopeTierBehavior,
)
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.workspace import WorkspaceInfo


class _Cfg:
    """Minimal config exposing personas with scope_behavior opt-in (FR-010)."""

    def __init__(self) -> None:
        tier = ScopeTierBehavior(max_tool_calls=5, prompt_addon="x")
        self.personas = PersonasConfig(
            security=PersonaConfig(
                instructions="SECURITY BASE",
                scope_behavior=ScopeBehavior(skim=tier),
            ),
        )


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        pass

    async def find_pr_for_issue(self, issue_id: str) -> dict[str, str] | None:
        return None


class _RecordingService:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def check_health(self) -> dict[str, Any]:
        return {"status": "accepted"}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "sess-1"}


class _WorkspaceManager:
    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        return WorkspaceInfo(
            path=Path("/tmp/fake-ws/repo"),
            branch="coordinare/ITEM_1/test-card",
            repo_url="https://github.com/acme/repo.git",
            github_token="test-token",
        )

    async def teardown(self, path: Path) -> None:
        pass


@pytest.mark.asyncio
async def test_skip_advances_lifecycle() -> None:
    """``depth: skip`` must advance past the persona without invoking it.

    Scenario: lifecycle = [reviewing, security, qa].  Current stage is
    ``security`` and the slice marks security as ``skip``.  After dispatch,
    the security service must NOT have been dispatched to, and
    ``performer_stage`` must have advanced past security.
    """
    security_svc = _RecordingService()
    qa_svc = _RecordingService()

    card: dict[str, Any] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_1",
        "pr_url": "https://github.com/acme/repo/pull/42",
        "pr_node_id": "PR_kwDO_42",
    }
    session = {
        "card_id": "ITEM_1",
        "current_card": card,
        "persona_scope": {
            "computed_at": "2026-05-27T00:00:00Z",
            "cycle_index": 1,
            "classifier_model": "test/model",
            "head_sha": "deadbeef",
            "files_summary": [],
            "personas": {
                "security": {"depth": "skip", "focus": "no security-sensitive paths", "overrides": []},
            },
        },
    }

    state = initial_state()
    state["config"] = _Cfg()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["reviewing", "security", "qa"]
    state["performer_services"] = {"security": security_svc, "qa": qa_svc}

    await dispatch_performer(state)

    # security must NOT have been dispatched to — skipped per scope slice.
    assert security_svc.dispatched == []
    # And the pipeline must have advanced past security.
    assert state.get("performer_stage") != "security"
