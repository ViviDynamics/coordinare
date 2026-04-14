"""Unit tests for monitor_performer node (019-performer-lifecycle, T015).

Tests cover lifecycle advancement, terminal success states, error/blocked
handling, session_expired routing, and TransportError handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.graph.nodes.monitor_performer import (
    TERMINAL_SUCCESS_STATES,
    _advance_stage,
    monitor_performer,
)
from coordinare.graph.state import initial_state
from coordinare.transport.base import TransportError

# ---------------------------------------------------------------------------
# Mock helpers (mirrors test_monitor_agent.py patterns)
# ---------------------------------------------------------------------------


class _GitHub:
    """Tracks move_card calls."""

    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _Performer:
    """Returns a configurable check_status response."""

    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


class _PerformerTransportError:
    """Raises TransportError from check_status."""

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        raise TransportError("connection refused")


class _WorkspaceManager:
    """Stub WorkspaceManager that records teardown calls."""

    def __init__(self) -> None:
        self.teardown_calls: list[Path] = []

    async def teardown(self, path: Path) -> None:
        self.teardown_calls.append(path)


# ---------------------------------------------------------------------------
# Helper to build state with performer_services for a given stage/sequence
# ---------------------------------------------------------------------------


def _make_state(
    *,
    service: object,
    stage: str = "implementing",
    sequence: list[str] | None = None,
    card: dict | None = None,
    github: object | None = None,
) -> dict:
    """Return a state dict pre-configured with performer_services and lifecycle_sequence."""
    state = initial_state()
    state["performer_services"] = {stage: service}
    state["performer_stage"] = stage
    state["lifecycle_sequence"] = sequence or [stage]
    state["current_card"] = card or {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    if github is not None:
        state["github_service"] = github
    return state


# ---------------------------------------------------------------------------
# T015-1: pr_opened triggers advancement to next stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_opened_advances_to_next_stage() -> None:
    """pr_opened on first of 2+ stages advances performer_stage and resets dispatch."""
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


# ---------------------------------------------------------------------------
# T015-2: advancement from final stage transitions to monitoring_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_stage_pr_opened_transitions_to_monitoring_pr() -> None:
    """pr_opened on the only/final stage transitions to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        card={"id": "ITEM_1", "status": "IN_PROGRESS"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/42"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_42"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["previous_status"] == "IN_PROGRESS"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls


# ---------------------------------------------------------------------------
# T015-3: error status sets phase="blocked", does NOT advance performer_stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_status_blocks_without_advancing_stage() -> None:
    """error status sets phase='blocked' and does NOT advance performer_stage."""
    service = _Performer({"status": "error", "reason": "Compilation failed"})
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "implementing"  # NOT advanced
    assert any("Compilation failed" in q for q in result["open_questions"])


# ---------------------------------------------------------------------------
# T015-4: in-progress returns unchanged state with phase="monitoring_performer"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_returns_monitoring_performer() -> None:
    """Working/in-progress status keeps phase='monitoring_performer'."""
    service = _Performer({"status": "working"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"
    # performer_stage unchanged
    assert result["performer_stage"] == "implementing"


# ---------------------------------------------------------------------------
# T015-5: all terminal success states are recognized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_SUCCESS_STATES))
async def test_all_terminal_success_states_trigger_advancement(terminal_status: str) -> None:
    """Every member of TERMINAL_SUCCESS_STATES triggers _advance_stage."""
    service = _Performer({
        "status": terminal_status,
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    # All terminal states on a non-final stage should advance
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


def test_terminal_success_states_contains_expected_members() -> None:
    """Verify the exact set of terminal success states."""
    expected = {
        "pr_opened",
        "plan_committed",
        "approved",
        "security_passed",
        "qa_passed",
        "docs_committed",
        "assessment_complete",
    }
    assert expected == TERMINAL_SUCCESS_STATES


# ---------------------------------------------------------------------------
# T015-6 / SC-001: 4 configured roles — full sequence advancement ending in monitoring_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_role_lifecycle_full_advancement() -> None:
    """4 roles (implementer, reviewer, security, QA) — advance through all, ending in monitoring_pr."""
    gh = _GitHub()
    roles = ["implementing", "reviewing", "security", "qa"]

    # Build performer services — each role gets its own service
    services = {}
    for role in roles:
        services[role] = _Performer({
            "status": "pr_opened",
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_node_id": "PR_NODE_99",
        })

    state = initial_state()
    state["performer_services"] = services
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = roles
    state["current_card"] = {"id": "ITEM_SC001", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    # Stage 1: implementing -> reviewing
    result = await monitor_performer(state)
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch: set phase back, set dispatch, keep stage
    result["agent_dispatch"] = {"session_id": "s2"}

    # Stage 2: reviewing -> security
    result = await monitor_performer(result)
    assert result["performer_stage"] == "security"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch
    result["agent_dispatch"] = {"session_id": "s3"}

    # Stage 3: security -> qa
    result = await monitor_performer(result)
    assert result["performer_stage"] == "qa"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch
    result["agent_dispatch"] = {"session_id": "s4"}

    # Stage 4 (final): qa -> monitoring_pr
    result = await monitor_performer(result)
    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/99"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_99"
    assert ("ITEM_SC001", "IN_REVIEW") in gh.move_calls


# ---------------------------------------------------------------------------
# T015-7: blocked status with questions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_status_with_questions() -> None:
    """Blocked with questions sets phase='blocked' and populates open_questions."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What API key?", "Which region?"],
    })
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What API key?", "Which region?"]


@pytest.mark.asyncio
async def test_blocked_status_without_questions() -> None:
    """Blocked with no questions list sets open_questions to empty list."""
    service = _Performer({"status": "blocked"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == []


# ---------------------------------------------------------------------------
# T015-8: session_expired handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_expired_no_pr_requeues_to_idle() -> None:
    """session_expired with no PR requeues card to TODO and sets phase='idle'."""
    gh = _GitHub()
    service = _Performer({"status": "session_expired", "reason": "timeout"})
    state = _make_state(
        service=service,
        card={"id": "ITEM_1"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    assert ("ITEM_1", "TODO") in gh.move_calls


@pytest.mark.asyncio
async def test_session_expired_with_pr_resumes_monitoring_pr() -> None:
    """session_expired with pr_node_id set transitions to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({"status": "session_expired"})
    state = _make_state(
        service=service,
        card={"id": "ITEM_1", "pr_node_id": "PR_NODE_1"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    # Should NOT move to TODO when PR exists
    assert ("ITEM_1", "TODO") not in gh.move_calls


@pytest.mark.asyncio
async def test_session_expired_preserves_open_questions_to_clarifications() -> None:
    """Open questions are saved to card_clarifications before clearing."""
    service = _Performer({"status": "session_expired"})
    state = _make_state(service=service, card={"id": "ITEM_1"})
    state["open_questions"] = ["Pending question"]

    result = await monitor_performer(state)

    assert result["open_questions"] == []
    assert len(result["card_clarifications"]) == 1
    assert result["card_clarifications"][0]["questions"] == ["Pending question"]
    assert result["card_clarifications"][0]["answer"] == ""


# ---------------------------------------------------------------------------
# T015-9: TransportError -> system_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_routes_to_system_error() -> None:
    """TransportError from check_status sets phase='system_error'."""
    service = _PerformerTransportError()
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert result["system_error_reason"] is not None
    assert "TransportError" in result["system_error_reason"]
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_transport_error_resets_stale_notified_state() -> None:
    """Stale system_error_notified=True is reset before incrementing."""
    service = _PerformerTransportError()
    state = _make_state(service=service)
    state["system_error_count"] = 5
    state["system_error_notified"] = True

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    # Count was reset to 0, then incremented to 1
    assert result["system_error_count"] == 1
    assert result["system_error_notified"] is False


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_service_and_no_card_returns_idle() -> None:
    """No performer service and no card -> idle."""
    state = initial_state()

    result = await monitor_performer(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_fallback_to_legacy_agent_service() -> None:
    """When performer_services is empty, falls back to agent_service."""
    service = _Performer({"status": "working"})
    state = initial_state()
    state["performer_services"] = {}
    state["agent_service"] = service
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_advancement_mid_sequence_resets_dispatch_state() -> None:
    """Advancing to a non-final stage resets agent_dispatch and agent_dispatch_at."""
    service = _Performer({
        "status": "plan_committed",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing", "qa"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_final_stage_missing_pr_fields_routes_to_system_error() -> None:
    """Final stage pr_opened with missing pr_url/pr_node_id routes to system_error."""
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": None,
        "pr_node_id": None,
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    assert ("ITEM_1", "IN_REVIEW") not in gh.move_calls


@pytest.mark.asyncio
async def test_workspace_teardown_on_terminal_state() -> None:
    """Workspace teardown is called on terminal state (pr_opened final stage)."""
    wm = _WorkspaceManager()
    fake_ws = Path("/tmp/fake-ws")
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        github=gh,
    )
    state["workspace_manager"] = wm
    state["workspace_path"] = fake_ws

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert wm.teardown_calls == [fake_ws]
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


@pytest.mark.asyncio
async def test_no_workspace_teardown_when_still_working() -> None:
    """Workspace is NOT torn down while performer is still working."""
    wm = _WorkspaceManager()
    fake_ws = Path("/tmp/fake-ws")
    service = _Performer({"status": "working"})
    state = _make_state(service=service)
    state["workspace_manager"] = wm
    state["workspace_path"] = fake_ws

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert wm.teardown_calls == []
    assert result["workspace_path"] == fake_ws


@pytest.mark.asyncio
async def test_events_accumulated_from_status() -> None:
    """Performer events are accumulated (capped at 100)."""
    service = _Performer({"status": "working", "events": [{"type": "new_event"}]})
    state = _make_state(service=service)
    state["performer_events"] = [{"type": "old_event"}]

    result = await monitor_performer(state)

    assert result["performer_events"] == [{"type": "old_event"}, {"type": "new_event"}]


@pytest.mark.asyncio
async def test_metrics_stored_from_status() -> None:
    """Performer metrics are updated from status."""
    service = _Performer({"status": "working", "metrics": {"cpu": 42}})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["performer_metrics"] == {"cpu": 42}


@pytest.mark.asyncio
async def test_error_status_includes_reason_in_open_questions() -> None:
    """Error status includes the reason in open_questions."""
    service = _Performer({"status": "error", "reason": "Out of memory"})
    state = _make_state(
        service=service,
        stage="reviewing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert len(result["open_questions"]) == 1
    assert "Out of memory" in result["open_questions"][0]
    assert "reviewing" in result["open_questions"][0]


@pytest.mark.asyncio
async def test_error_status_without_reason() -> None:
    """Error status with no reason still produces a meaningful open_questions entry."""
    service = _Performer({"status": "error"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert len(result["open_questions"]) == 1
    assert "error" in result["open_questions"][0].lower()


# ---------------------------------------------------------------------------
# 020 — plan_path persistence via _advance_stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_path_persisted_to_card_mid_sequence() -> None:
    """Architect returns plan_committed with plan_path — card gets plan_path
    when advancing to the next stage (mid-sequence, not final)."""
    service = _Performer({
        "status": "plan_committed",
        "plan_path": "docs/plan.md",
    })
    state = _make_state(
        service=service,
        stage="architecting",
        sequence=["architecting", "implementing"],
        card={"id": "ITEM_1", "status": "IN_PROGRESS"},
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["current_card"]["plan_path"] == "docs/plan.md"


@pytest.mark.asyncio
async def test_final_stage_plan_path_persisted() -> None:
    """Single-stage lifecycle (architecting only) — plan_committed with
    plan_path and pr_url persists plan_path on the card when transitioning
    to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({
        "status": "plan_committed",
        "plan_path": "docs/architecture.md",
        "pr_url": "https://github.com/org/repo/pull/7",
        "pr_node_id": "PR_NODE_7",
    })
    state = _make_state(
        service=service,
        stage="architecting",
        sequence=["architecting"],
        card={"id": "ITEM_2", "status": "IN_PROGRESS"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["plan_path"] == "docs/architecture.md"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/7"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_7"


def test_advance_stage_plan_path_none_status() -> None:
    """_advance_stage with status=None does not crash and does not set
    plan_path on the card."""
    state = initial_state()
    state["lifecycle_sequence"] = ["architecting", "implementing"]
    state["performer_stage"] = "architecting"
    state["current_card"] = {"id": "ITEM_3", "status": "IN_PROGRESS"}

    updates = _advance_stage(state, status=None)

    assert updates["performer_stage"] == "implementing"
    assert updates["phase"] == "dispatching"
    # No current_card update should be present when status is None.
    assert "current_card" not in updates


# ---------------------------------------------------------------------------
# 021 — changes_requested handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_requested_routes_to_implementer() -> None:
    """changes_requested stores comments as relay_feedback and re-dispatches implementer."""
    state = initial_state()
    svc = _Performer(response={"status": "changes_requested", "comments": [
        {"file": "src/main.py", "line": 10, "body": "Missing null check"},
    ]})
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == [
        {"file": "src/main.py", "line": 10, "body": "Missing null check"},
    ]
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_changes_requested_does_not_advance_lifecycle() -> None:
    """changes_requested does NOT advance to the next role — it routes back."""
    state = initial_state()
    svc = _Performer(response={"status": "changes_requested", "comments": []})
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    # Should route back to implementing, NOT advance to security
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# 022 — security_failed handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_failed_routes_findings_to_implementer() -> None:
    """security_failed with implementer-routed findings resets to implementing."""
    state = initial_state()
    findings = [{"severity": "critical", "category": "injection", "routing": "implementer"}]
    svc = _Performer(response={"status": "security_failed", "findings": findings})
    state["performer_services"] = {"security": svc}
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "security", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == findings


@pytest.mark.asyncio
async def test_security_failed_routes_architecture_findings_to_architect() -> None:
    """security_failed with architect-routed findings resets to architecting."""
    state = initial_state()
    findings = [{"severity": "high", "category": "insecure_design", "routing": "architect"}]
    svc = _Performer(response={"status": "security_failed", "findings": findings})
    state["performer_services"] = {"security": svc}
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["architecting", "implementing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "architecting"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == findings


# ---------------------------------------------------------------------------
# 023 — qa_failed handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_failed_routes_to_implementer() -> None:
    """qa_failed routes failures to implementer for remediation."""
    state = initial_state()
    failures = [{"criterion": "Login works", "expected": "200", "actual": "500", "test": "test_login"}]
    svc = _Performer(response={"status": "qa_failed", "failures": failures})
    state["performer_services"] = {"qa": svc}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == failures


# ---------------------------------------------------------------------------
# 027 — Per-role timeout tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_timeout_blocks_card() -> None:
    """When role timeout is exceeded, card is blocked with timeout message."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=600)
    state["role_timeouts"] = {"implementing": 300}  # 5 min timeout, 10 min elapsed

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert any("timed out" in q for q in result.get("open_questions", []))


@pytest.mark.asyncio
async def test_no_timeout_when_within_limit() -> None:
    """When elapsed time is within timeout, normal monitoring continues."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=10)
    state["role_timeouts"] = {"implementing": 300}  # 5 min timeout, 10s elapsed

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # still working


@pytest.mark.asyncio
async def test_no_timeout_when_not_configured() -> None:
    """No role_timeouts entry → no timeout enforcement."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(hours=2)
    state["role_timeouts"] = {}  # no timeout configured

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # no enforcement


# ---------------------------------------------------------------------------
# 032 — Active Board Reconciliation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_consistent_column_no_action() -> None:
    """Card in expected column → normal monitoring continues."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": ["ITEM_1"], "TODO": [], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # no reconciliation


@pytest.mark.asyncio
async def test_reconcile_backward_move_to_todo() -> None:
    """Card moved backward to TODO → idle."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": ["ITEM_1"], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_reconcile_forward_to_done() -> None:
    """Card moved to DONE → idle with cleared card."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": [], "DONE": ["ITEM_1"]}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None


@pytest.mark.asyncio
async def test_reconcile_moved_to_blocked() -> None:
    """Card moved to BLOCKED → blocked phase."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "BLOCKED": ["ITEM_1"], "TODO": []}

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_reconcile_no_board_snapshot_skips() -> None:
    """No board_snapshot → reconciliation skipped, normal monitoring."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    # board_snapshot defaults to {} from initial_state — reconciliation skipped

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # reconciliation skipped


@pytest.mark.asyncio
async def test_reconcile_card_not_found_in_populated_board() -> None:
    """Card not in any column of a populated board → idle."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_GONE", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": ["OTHER_CARD"], "TODO": [], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None


# ---------------------------------------------------------------------------
# 030 — Live Requirement Sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requirement_change_detected_warn_policy() -> None:
    """Description change → requirements_changed=True, phase stays monitoring."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "NEW description"})

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["requirements_changed"] is True
    assert result["phase"] == "monitoring_performer"  # warn doesn't re-dispatch


@pytest.mark.asyncio
async def test_requirement_unchanged_no_flag() -> None:
    """Same description → requirements_changed remains False."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "Same description"})

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "Same description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["requirements_changed"] is False


@pytest.mark.asyncio
async def test_requirement_change_redispatch_policy() -> None:
    """re-dispatch policy → phase set to dispatching with updated card."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "UPDATED requirements"})

    config = MagicMock()
    config.requirement_change_policy = "re-dispatch"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD requirements", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["description"] == "UPDATED requirements"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_requirement_check_api_failure_no_crash() -> None:
    """GitHub API failure → no crash, monitoring continues."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(side_effect=ConnectionError("api down"))

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "desc", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # continues despite error


@pytest.mark.asyncio
async def test_requirement_change_ignore_policy() -> None:
    """ignore policy → requirement change check skipped, no warning, performer continues."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "NEW description"})

    config = MagicMock()
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    # ignore policy skips the check entirely — requirements_changed stays False
    assert result["requirements_changed"] is False
    assert result["phase"] == "monitoring_performer"
    # and no GitHub requirement refetch should be performed
    github.get_issue_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_requirement_sync_skipped_on_terminal_status() -> None:
    """Terminal performer status → requirement sync skipped, no re-dispatch."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "CHANGED"})
    github.move_card = AsyncMock()

    config = MagicMock()
    config.requirement_change_policy = "re-dispatch"

    state = initial_state()
    svc = _Performer(response={"status": "pr_opened", "pr_url": "https://github.com/test/1", "pr_node_id": "PR_1"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    # Terminal status takes priority — no re-dispatch even with re-dispatch policy
    assert result["phase"] != "dispatching"
    github.get_issue_details.assert_not_awaited()


# ---------------------------------------------------------------------------
# 031 — Human Override Controls tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_skip_advances_stage() -> None:
    """pending_override skip → advances to next role in lifecycle."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "skip"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_skip_last_role_transitions_to_monitoring_pr() -> None:
    """pending_override skip on final role → monitoring_pr."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["pending_override"] = {"action": "skip"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "monitoring_pr"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_restart_sets_target_stage() -> None:
    """pending_override restart → sets performer_stage to target."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "restart", "target_stage": "implementing"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_restart_invalid_role_noop() -> None:
    """pending_override restart with invalid role → no stage change."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["pending_override"] = {"action": "restart", "target_stage": "nonexistent"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "reviewing"  # unchanged
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_veto_blocks_card() -> None:
    """pending_override veto → phase set to blocked."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["current_card"] = {"id": "ITEM_1"}
    state["pending_override"] = {"action": "veto"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "blocked"
    assert "vetoed" in result["open_questions"][0].lower()
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_none_returns_none() -> None:
    """No pending_override → returns None (no-op)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["pending_override"] = None

    result = _apply_pending_override(state)

    assert result is None


@pytest.mark.asyncio
async def test_monitor_performer_applies_override_before_polling() -> None:
    """monitor_performer consumes pending_override before checking status."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc, "reviewing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["pending_override"] = {"action": "skip"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_dashboard_precedence_over_pr_comment() -> None:
    """Dashboard override already set takes precedence (FR-010)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    # Dashboard set veto; PR comment would have set skip — but dashboard wins
    # because it's already in pending_override when the graph runs
    state["pending_override"] = {"action": "veto"}
    state["current_card"] = {"id": "ITEM_1"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_override_on_idle_phase_noop() -> None:
    """Override when card is idle — helper still applies it (API guards prevent this)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["phase"] = "idle"
    state["pending_override"] = {"action": "skip"}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]

    result = _apply_pending_override(state)

    # Helper applies it; the API endpoints prevent setting overrides on idle
    assert result is not None
    assert result["performer_stage"] == "reviewing"


@pytest.mark.asyncio
async def test_override_restart_earlier_than_first_role() -> None:
    """restart-from to a valid early role works correctly."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "restart", "target_stage": "implementing"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# 034 — Cost & Token Tracking tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_accumulation_across_polls() -> None:
    """tokens_processed accumulates into card_tokens_total across polls."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 1500}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 1500

    # Poll again with more tokens
    svc2 = _Performer(response={"status": "working", "metrics": {"tokens_processed": 2000}})
    result["performer_services"] = {"implementing": svc2}
    result["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(result)
    assert result["card_tokens_total"] == 3500


@pytest.mark.asyncio
async def test_no_tokens_field_leaves_total_unchanged() -> None:
    """Missing tokens_processed leaves card_tokens_total unchanged."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500


@pytest.mark.asyncio
async def test_negative_tokens_clamped_to_zero() -> None:
    """Negative tokens_processed is clamped to 0."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": -100}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500  # unchanged


@pytest.mark.asyncio
async def test_cost_estimate_calculated() -> None:
    """card_cost_estimate is calculated from tokens and config rate."""
    from unittest.mock import MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = None
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 1_000_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config

    result = await monitor_performer(state)
    assert result["card_cost_estimate"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_budget_exceeded_notification_fires_once() -> None:
    """Budget exceeded dispatches notification exactly once."""
    from unittest.mock import AsyncMock, MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = 1.0
    config.requirement_change_policy = "ignore"

    notification_service = AsyncMock()

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 500_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config
    state["notification_service"] = notification_service

    # First poll: cost = $1.50, exceeds $1.00 budget
    result = await monitor_performer(state)
    assert result["card_budget_alert_sent"] is True
    assert notification_service.dispatch.await_count == 1

    # Second poll: more tokens, but alert already sent
    svc2 = _Performer(response={"status": "working", "metrics": {"tokens_processed": 200_000}})
    result["performer_services"] = {"implementing": svc2}
    result["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(result)
    assert notification_service.dispatch.await_count == 1  # no duplicate


@pytest.mark.asyncio
async def test_no_budget_configured_skips_check() -> None:
    """No cost_budget_per_card → no budget check."""
    from unittest.mock import MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = None
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 10_000_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config

    result = await monitor_performer(state)
    assert result["card_budget_alert_sent"] is False


@pytest.mark.asyncio
async def test_float_tokens_processed_rejected() -> None:
    """Float tokens_processed is rejected (spec requires integers only)."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 100.5}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 0

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 0  # float rejected


@pytest.mark.asyncio
async def test_non_numeric_tokens_ignored() -> None:
    """Non-numeric tokens_processed leaves card_tokens_total unchanged."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": "not-a-number"}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500  # unchanged


# ---------------------------------------------------------------------------
# Assessor performer — assessment_complete advances lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_complete_advances_to_next_stage() -> None:
    """assessment_complete on first stage advances to the next performer role."""
    service = _Performer({
        "status": "assessment_complete",
    })
    state = _make_state(
        service=service,
        stage="assessing",
        sequence=["assessing", "implementing"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_assessment_complete_is_in_terminal_success_states() -> None:
    """Confirm assessment_complete is recognised as a terminal success status."""
    assert "assessment_complete" in TERMINAL_SUCCESS_STATES


@pytest.mark.asyncio
async def test_assessor_blocked_routes_to_blocked() -> None:
    """When the assessor performer reports blocked, phase should be 'blocked'."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What is the acceptance criteria?"],
    })
    state = _make_state(
        service=service,
        stage="assessing",
        sequence=["assessing", "implementing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "assessing"  # NOT advanced
    assert "What is the acceptance criteria?" in result["open_questions"]
