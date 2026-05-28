"""E2E integration tests for the implementer CI gate (spec 075).

Stitches ``monitor_performer`` (gate evaluation) through to ``notify`` (PR
rollup comment) so the full implementer→reviewer hand-off path is covered
end-to-end on a single fake GitHub backend.

T060 — happy path through BOUNCE → relay_feedback → fixed → PASS.
T061 — pending CI HOLD → eventually green PASS (US2).
T062 — bounce loop hits ``max_bounces_per_head`` → ESCALATE (US1 escalate).
T069 — timing assertion: _evaluate_ci_gate returns within 3 s (SC-002).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from coordinare.config import CIGateConfig, PersonaScopeConfig
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.nodes.notify import (
    CI_GATE_ROLLUP_MARKER_PREFIX,
    notify,
)
from coordinare.graph.state import initial_state
from tests.utils.fake_notification import FakeNotificationService

# ---------------------------------------------------------------------------
# Fake GitHub backend — handles GraphQL rollup + REST issue-comments + post.
# ---------------------------------------------------------------------------


class _FakeGitHub:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}
        self.comments: list[dict[str, Any]] = []
        self.added: list[tuple[str, str]] = []
        self.move_calls: list[tuple[str, str]] = []
        self.latency_seconds: float = 0.0

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        return self.payload

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return list(self.comments)

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self.added.append((subject_id, body))
        self.comments.append({"body": body})
        return {"id": f"C{len(self.added)}"}


class _Performer:
    def __init__(self) -> None:
        self.response = {
            "status": "pr_opened",
            "pr_url": "https://github.com/org/repo/pull/42",
            "pr_node_id": "PR_NODE_42",
        }

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self.response


def _rollup(head_sha: str, contexts: list[dict[str, Any]], *, state: str = "PENDING") -> dict:
    pushed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": head_sha,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": head_sha,
                                "pushedDate": pushed,
                                "statusCheckRollup": {
                                    "state": state,
                                    "contexts": {"nodes": contexts},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": []},
        }
    }


def _state(gh: _FakeGitHub, *, max_bounces: int = 3,
           bounce_counter: dict[str, int] | None = None) -> dict[str, Any]:
    state = initial_state()
    state["performer_services"] = {"implementing": _Performer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {
        "id": "ITEM_1",
        "title": "Card",
        "status": "IN_PROGRESS",
        "pr_node_id": "PR_NODE_42",
        "issue_number": 42,
    }
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh
    state["notification_service"] = FakeNotificationService()
    state["bounce_counter"] = bounce_counter or {}

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=max_bounces),
        )

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


def _failing(head: str) -> dict:
    return _rollup(head, [
        {"__typename": "CheckRun", "name": "unit-tests",
         "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "name": "lint",
         "status": "COMPLETED", "conclusion": "SUCCESS"},
    ])


def _passing(head: str) -> dict:
    return _rollup(head, [
        {"__typename": "CheckRun", "name": "unit-tests",
         "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "lint",
         "status": "COMPLETED", "conclusion": "SUCCESS"},
    ], state="SUCCESS")


def _pending(head: str) -> dict:
    return _rollup(head, [
        {"__typename": "CheckRun", "name": "unit-tests",
         "status": "IN_PROGRESS", "conclusion": None},
        {"__typename": "CheckRun", "name": "lint",
         "status": "COMPLETED", "conclusion": "SUCCESS"},
    ])


# ---------------------------------------------------------------------------
# T060 — full path: BOUNCE → relay feedback → next head green → PASS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_path_bounce_then_pass() -> None:
    gh = _FakeGitHub()
    head1 = "a" * 40
    gh.payload = _failing(head1)
    state = _state(gh)

    # Cycle 1: bounce.
    result = await monitor_performer(state)
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"
    assert result["performer_stage"] == "implementing"
    relay = result.get("relay_feedback") or []
    assert relay and "unit-tests" in relay[-1]["body"]

    # Carry session state forward so notify sees the decision.
    state["active_sessions"] = {"ITEM_1": {
        "latest_ci_gate_decision": result["latest_ci_gate_decision"],
    }}
    state["phase"] = "monitoring_performer"
    await notify(state)
    assert len(gh.added) == 1
    assert "BOUNCE" in gh.added[0][1]
    assert CI_GATE_ROLLUP_MARKER_PREFIX in gh.added[0][1]

    # Cycle 2: performer pushed a fix; new head, all green.
    head2 = "b" * 40
    gh.payload = _passing(head2)
    state["bounce_counter"] = result.get("bounce_counter") or {}
    state["performer_stage"] = "implementing"
    state["phase"] = "dispatching"
    result2 = await monitor_performer(state)
    assert result2["latest_ci_gate_decision"]["verdict"] == "pass"
    assert result2["performer_stage"] == "reviewing"

    # PASS is silent — notify must not add another comment.
    state["active_sessions"] = {"ITEM_1": {
        "latest_ci_gate_decision": result2["latest_ci_gate_decision"],
    }}
    await notify(state)
    assert len(gh.added) == 1  # unchanged


# ---------------------------------------------------------------------------
# T061 — pending HOLD then green PASS (US2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_hold_then_pass() -> None:
    gh = _FakeGitHub()
    head = "c" * 40
    gh.payload = _pending(head)
    state = _state(gh)

    result = await monitor_performer(state)
    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    # HOLD keeps the card in implementing, no advance.
    assert result.get("performer_stage", "implementing") == "implementing"

    state["active_sessions"] = {"ITEM_1": {
        "latest_ci_gate_decision": result["latest_ci_gate_decision"],
    }}
    state["phase"] = "monitoring_performer"
    await notify(state)
    assert len(gh.added) == 1
    assert "HOLD" in gh.added[0][1]

    # Next cycle: checks finish green.
    gh.payload = _passing(head)
    result2 = await monitor_performer(state)
    assert result2["latest_ci_gate_decision"]["verdict"] == "pass"
    assert result2["performer_stage"] == "reviewing"


# ---------------------------------------------------------------------------
# T062 — bounce loop escalates at max_bounces_per_head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounce_loop_escalates() -> None:
    gh = _FakeGitHub()
    head = "d" * 40
    gh.payload = _failing(head)
    # Already at limit-1 → next bounce escalates.
    state = _state(gh, max_bounces=3, bounce_counter={head: 2})

    result = await monitor_performer(state)
    assert result["latest_ci_gate_decision"]["verdict"] == "escalate"
    assert result["phase"] == "blocked"

    state["active_sessions"] = {"ITEM_1": {
        "latest_ci_gate_decision": result["latest_ci_gate_decision"],
    }}
    state["phase"] = "monitoring_performer"
    await notify(state)
    assert len(gh.added) == 1
    assert "ESCALATE" in gh.added[0][1]


# ---------------------------------------------------------------------------
# T069 — timing: _evaluate_ci_gate returns within SC-002 p95 budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_within_budget() -> None:
    gh = _FakeGitHub()
    gh.payload = _passing("e" * 40)
    gh.latency_seconds = 0.05  # mock GraphQL latency
    state = _state(gh)

    t0 = time.monotonic()
    result = await monitor_performer(state)
    elapsed = time.monotonic() - t0

    assert result["latest_ci_gate_decision"]["verdict"] == "pass"
    assert elapsed < 3.0, f"gate eval took {elapsed:.3f}s, exceeds SC-002 3s budget"
