"""Unit tests for dispatch_performer (019-performer-lifecycle T014)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinare.config import PersonaConfig, PersonasConfig
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS
from coordinare.workspace import WorkspaceInfo

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _GitHub:
    def __init__(
        self,
        *,
        recovered_pr: dict[str, str] | None = None,
        closed_pr_count: int = 0,
        closed_pr_delay_seconds: float = 0.0,
        move_blocked_error: Exception | None = None,
    ) -> None:
        self.move_calls: list[tuple[str, str]] = []
        self.find_pr_calls: list[str] = []
        self._recovered_pr = recovered_pr
        self._closed_pr_count = closed_pr_count
        self._closed_pr_delay_seconds = closed_pr_delay_seconds
        self._move_blocked_error = move_blocked_error

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))
        if status == "BLOCKED" and self._move_blocked_error is not None:
            raise self._move_blocked_error

    async def find_pr_for_issue(self, issue_id: str) -> dict[str, str] | None:
        self.find_pr_calls.append(issue_id)
        return self._recovered_pr

    async def count_closed_prs_for_issue(self, issue_id: str) -> int:
        if self._closed_pr_delay_seconds > 0:
            import asyncio as _aio
            await _aio.sleep(self._closed_pr_delay_seconds)
        return self._closed_pr_count


class _Service:
    """Healthy service that records dispatch_card calls."""

    def __init__(self, *, health_status: str = "accepted") -> None:
        self._health_status = health_status
        self.dispatched: list[dict[str, Any]] = []
        self.last_workspace_info: WorkspaceInfo | None = None

    async def check_health(self) -> dict[str, Any]:
        return {"status": self._health_status}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: WorkspaceInfo | None = None
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        self.last_workspace_info = workspace_info
        return {"status": "accepted", "session_id": "sess-1"}


class _ServiceHealthError:
    """Service that reports health status 'error'."""

    async def check_health(self) -> dict[str, Any]:
        return {"status": "error", "reason": "disk full"}

    async def dispatch_card(self, card_context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Should not dispatch when health status is error")


class _ServiceUnreachable:
    """Service whose health check raises (simulating unreachable agent)."""

    async def check_health(self) -> dict[str, Any]:
        raise ConnectionError("connection refused")

    async def dispatch_card(self, card_context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Should not dispatch when unreachable")


class _WorkspaceManager:
    """Stub workspace manager that returns a valid WorkspaceInfo."""

    def __init__(self, workspace_path: Path = Path("/tmp/fake-ws/repo")) -> None:
        self._path = workspace_path

    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        return WorkspaceInfo(
            path=self._path,
            branch="coordinare/ITEM_1/test-card",
            repo_url="https://github.com/acme/repo.git",
            github_token="test-token",
        )

    async def teardown(self, path: Path) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_state(**overrides: Any) -> dict[str, Any]:
    """Return an initial state pre-configured with common test prerequisites."""
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()
    state.update(overrides)
    return state


def _make_config_with_persona(role: str, instructions: str) -> Any:
    """Build a fake config object whose personas.<role>.instructions is set."""
    kwargs = {role: PersonaConfig(instructions=instructions)}
    personas = PersonasConfig(**kwargs)

    class _FakeConfig:
        pass

    cfg = _FakeConfig()
    cfg.personas = personas
    return cfg


# ---------------------------------------------------------------------------
# T014-1: dispatch resolves correct service based on performer_stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_resolves_correct_service_for_stage() -> None:
    """When performer_services has multiple stages, only the one matching
    performer_stage should have its dispatch_card called."""
    implementing_svc = _Service()
    reviewing_svc = _Service()
    security_svc = _Service()

    state = _base_state(
        performer_services={
            "implementing": implementing_svc,
            "reviewing": reviewing_svc,
            "security": security_svc,
        },
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing", "security"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    # Only the reviewing service should have been dispatched to.
    assert len(reviewing_svc.dispatched) == 1
    assert len(implementing_svc.dispatched) == 0
    assert len(security_svc.dispatched) == 0


@pytest.mark.asyncio
async def test_dispatch_resolves_first_stage_service() -> None:
    """Verify dispatch works for the first stage in the sequence."""
    implementing_svc = _Service()
    reviewing_svc = _Service()

    state = _base_state(
        performer_services={
            "implementing": implementing_svc,
            "reviewing": reviewing_svc,
        },
        performer_stage="implementing",
        lifecycle_sequence=["implementing", "reviewing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(implementing_svc.dispatched) == 1
    assert len(reviewing_svc.dispatched) == 0


@pytest.mark.asyncio
async def test_reviewing_stage_recovers_missing_pr_context_from_issue() -> None:
    """Reviewer dispatch recovers pr_url/pr_node_id from linked issue when missing."""
    reviewing_svc = _Service()
    github = _GitHub(recovered_pr={
        "pr_url": "https://github.com/acme/repo/pull/42",
        "pr_node_id": "PR_kwDO_42",
    })
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "IN_PROGRESS", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"reviewing": reviewing_svc},
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(reviewing_svc.dispatched) == 1
    assert reviewing_svc.dispatched[0]["pr_url"] == "https://github.com/acme/repo/pull/42"
    assert reviewing_svc.dispatched[0]["pr_node_id"] == "PR_kwDO_42"
    assert github.find_pr_calls == ["ISSUE_NODE_1"]


@pytest.mark.asyncio
async def test_reviewing_stage_missing_pr_context_falls_back_to_implementing() -> None:
    """When PR recovery fails, reroute PR-dependent stage to implementing."""
    implementing_svc = _Service()
    reviewing_svc = _Service()
    github = _GitHub(recovered_pr=None)
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "IN_PROGRESS", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"implementing": implementing_svc, "reviewing": reviewing_svc},
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    assert len(reviewing_svc.dispatched) == 0
    assert len(implementing_svc.dispatched) == 0


@pytest.mark.asyncio
async def test_implementing_stage_blocks_when_closed_pr_limit_reached() -> None:
    """Configurable PR-attempt limit blocks fresh implementer dispatches."""
    from coordinare.config import ProjectConfiguration

    implementing_svc = _Service()
    github = _GitHub(closed_pr_count=4)
    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        max_closed_pr_attempts_per_issue=3,
    )
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "TODO", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"implementing": implementing_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in github.move_calls
    assert any("closed PR attempts" in q for q in result.get("open_questions", []))
    assert len(implementing_svc.dispatched) == 0


@pytest.mark.asyncio
async def test_closed_pr_limit_logs_move_card_blocked_error_details() -> None:
    """BLOCKED move failures should log structured error details for triage."""
    from unittest.mock import patch

    from coordinare.config import ProjectConfiguration

    implementing_svc = _Service()
    github = _GitHub(closed_pr_count=4, move_blocked_error=RuntimeError("permission denied"))
    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        max_closed_pr_attempts_per_issue=3,
    )
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "TODO", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"implementing": implementing_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    with patch("coordinare.graph.nodes.dispatch_performer.logger.warning") as warning_mock:
        result = await dispatch_performer(state)

    assert result["phase"] == "blocked"
    assert any("closed PR attempts" in q for q in result.get("open_questions", []))
    assert any(
        call.args and call.args[0] == "dispatch_performer.move_card_to_blocked_failed"
        and call.kwargs.get("card_id") == "ITEM_1"
        and "permission denied" in str(call.kwargs.get("error"))
        for call in warning_mock.call_args_list
    )


@pytest.mark.asyncio
async def test_implementing_stage_not_blocked_when_open_pr_recovered() -> None:
    """Open PR recovery bypasses closed-PR limit guard."""
    from coordinare.config import ProjectConfiguration

    implementing_svc = _Service()
    github = _GitHub(
        recovered_pr={
            "pr_url": "https://github.com/acme/repo/pull/42",
            "pr_node_id": "PR_kwDO_42",
        },
        closed_pr_count=99,
    )
    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        max_closed_pr_attempts_per_issue=3,
    )
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "TODO", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"implementing": implementing_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(implementing_svc.dispatched) == 1
    assert implementing_svc.dispatched[0]["pr_url"] == "https://github.com/acme/repo/pull/42"
    assert implementing_svc.dispatched[0]["pr_node_id"] == "PR_kwDO_42"


@pytest.mark.asyncio
async def test_implementing_stage_closed_pr_count_timeout_does_not_block_dispatch() -> None:
    """Timeout counting closed PRs should not hang or block implementer dispatch."""
    from coordinare.config import ProjectConfiguration

    implementing_svc = _Service()
    github = _GitHub(closed_pr_count=99, closed_pr_delay_seconds=2.0)
    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        transport_timeout_seconds=1,
        max_closed_pr_attempts_per_issue=3,
    )
    state = _base_state(
        current_card={"id": "ITEM_1", "status": "TODO", "issue_id": "ISSUE_NODE_1"},
        github_service=github,
        performer_services={"implementing": implementing_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(implementing_svc.dispatched) == 1
    assert ("ITEM_1", "BLOCKED") not in github.move_calls


# ---------------------------------------------------------------------------
# T014-2: skip when service is None (advances to next stage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_service_none_advances_to_next_stage() -> None:
    """When performer_stage references a stage with no service and no legacy
    agent_service, _advance_stage is called and performer_stage changes."""
    implementing_svc = _Service()

    state = _base_state(
        performer_services={
            "implementing": implementing_svc,
            # "reviewing" is intentionally absent
        },
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing", "security"],
        agent_service=None,
    )

    result = await dispatch_performer(state)

    # _advance_stage should have moved to "security" (the next stage)
    assert result["performer_stage"] == "security"
    assert result["phase"] == "dispatching"
    # No service should have been dispatched to.
    assert len(implementing_svc.dispatched) == 0


@pytest.mark.asyncio
async def test_skip_last_stage_transitions_to_monitoring_pr() -> None:
    """When the last stage has no service, _advance_stage transitions to
    monitoring_pr since there are no more stages remaining."""
    state = _base_state(
        performer_services={},
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing"],
        agent_service=None,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_pr"


# ---------------------------------------------------------------------------
# T014-3: health check failure sets blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_error_sets_blocked() -> None:
    """When the health check returns status 'error', phase is set to 'blocked'
    and open_questions includes a descriptive message."""
    svc = _ServiceHealthError()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "error"
    assert any("health check failed" in q for q in result["open_questions"])
    assert any("implementing" in q for q in result["open_questions"])


# ---------------------------------------------------------------------------
# T014-4: persona instructions are injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_instructions_injected_in_card_context() -> None:
    """The card_context passed to service.dispatch_card must contain
    persona_instructions for the resolved stage/role."""
    svc = _Service()
    config = _make_config_with_persona("implementer", "Always write unit tests first.")

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert "persona_instructions" in card_ctx
    assert card_ctx["persona_instructions"].startswith("Always write unit tests first.")
    assert "in English" in card_ctx["persona_instructions"]


@pytest.mark.asyncio
async def test_persona_instructions_default_when_empty() -> None:
    """When persona config has empty instructions, the default instructions
    for the role should be injected instead."""
    svc = _Service()
    config = _make_config_with_persona("implementer", "")

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert card_ctx["persona_instructions"].startswith(DEFAULT_INSTRUCTIONS["implementer"])
    assert "in English" in card_ctx["persona_instructions"]


@pytest.mark.asyncio
async def test_persona_instructions_for_reviewer_stage() -> None:
    """Verify that the 'reviewing' stage maps to the 'reviewer' persona role
    and injects the correct instructions."""
    svc = _Service()
    config = _make_config_with_persona("reviewer", "Be thorough in reviews.")

    state = _base_state(
        performer_services={"reviewing": svc},
        performer_stage="reviewing",
        lifecycle_sequence=["reviewing"],
        config=config,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert card_ctx["persona_instructions"].startswith("Be thorough in reviews.")
    assert "in English" in card_ctx["persona_instructions"]


@pytest.mark.asyncio
async def test_persona_instructions_for_closing_review_stage() -> None:
    """042: Verify that the 'closing_review' stage maps to the 'closer'
    persona role — distinct from 'reviewing'/'reviewer' so the closing pass
    can carry its own instructions."""
    svc = _Service()
    config = _make_config_with_persona(
        "closer", "Verify prior threads were addressed; resolve and approve.",
    )

    state = _base_state(
        performer_services={"closing_review": svc},
        performer_stage="closing_review",
        lifecycle_sequence=["closing_review"],
        config=config,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert card_ctx["role"] == "closing_review"
    assert (
        card_ctx["persona_instructions"].startswith(
            "Verify prior threads were addressed; resolve and approve."
        )
    )


# ---------------------------------------------------------------------------
# T014-5: backward-compat with implementer-only lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backward_compat_no_performer_services_uses_agent_service() -> None:
    """When performer_services is empty but agent_service is set, dispatch
    falls back to agent_service (pre-019 backward compatibility)."""
    legacy_agent = _Service()

    state = _base_state(
        performer_services={},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        agent_service=legacy_agent,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(legacy_agent.dispatched) == 1


@pytest.mark.asyncio
async def test_backward_compat_performer_services_takes_precedence() -> None:
    """When both performer_services and agent_service are set, the service
    from performer_services is used (not the legacy fallback)."""
    performer_svc = _Service()
    legacy_agent = _Service()

    state = _base_state(
        performer_services={"implementing": performer_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        agent_service=legacy_agent,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(performer_svc.dispatched) == 1
    assert len(legacy_agent.dispatched) == 0


# ---------------------------------------------------------------------------
# T014-6: relay_feedback included in dispatch payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_feedback_included_in_dispatch_payload() -> None:
    """When state contains relay_feedback, the card_context passed to
    dispatch_card must include the feedback content."""
    svc = _Service()
    feedback = [
        {"reviewer": "alice", "comment": "Needs more tests"},
        {"reviewer": "bob", "comment": "LGTM"},
    ]

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        relay_feedback=feedback,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert "relay_feedback" in card_ctx
    assert card_ctx["relay_feedback"] == feedback


@pytest.mark.asyncio
async def test_relay_feedback_absent_when_state_has_none() -> None:
    """When relay_feedback is not in state (or is None/empty), the
    card_context should not contain relay_feedback."""
    svc = _Service()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )
    # Ensure relay_feedback is not set.
    state.pop("relay_feedback", None)

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_ctx = svc.dispatched[0]
    assert "relay_feedback" not in card_ctx


# ---------------------------------------------------------------------------
# T014-7: unreachable agent -> idle phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_agent_sets_idle_phase() -> None:
    """When the health check raises an exception (agent unreachable),
    phase is set to 'idle' (not 'blocked') so the graph retries later."""
    svc = _ServiceUnreachable()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_health_status"] == "unreachable"


@pytest.mark.asyncio
async def test_unreachable_agent_unknown_status_sets_idle() -> None:
    """When health check returns an unknown status string, phase becomes idle."""

    class _ServiceUnknownHealth:
        async def check_health(self) -> dict[str, Any]:
            return {"status": "unknown"}

        async def dispatch_card(self, card_context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Should not dispatch")

    svc = _ServiceUnknownHealth()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_health_status"] == "unknown"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_card_sets_idle() -> None:
    """When current_card is None, dispatch_performer transitions to idle."""
    state = initial_state()
    state["github_service"] = _GitHub()
    state["performer_stage"] = "implementing"
    # current_card stays None

    result = await dispatch_performer(state)
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_missing_github_service_sets_idle() -> None:
    """When github_service is None, dispatch_performer transitions to idle."""
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["performer_stage"] = "implementing"
    # github_service stays None

    result = await dispatch_performer(state)
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_missing_performer_stage_sets_idle() -> None:
    """When performer_stage is empty, dispatch_performer transitions to idle."""
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["performer_stage"] = ""

    result = await dispatch_performer(state)
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_success_resets_system_error_fields() -> None:
    """On successful dispatch from a fresh pickup (or after a prior card's
    budget was already exhausted), system_error_count and related fields reset."""
    svc = _Service()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        system_error_count=3,
        system_error_reason="previous failure",
        system_error_notified=True,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert result["system_error_count"] == 0
    assert result["system_error_last_at"] is None
    assert result["system_error_notified"] is False
    assert result["system_error_reason"] is None


@pytest.mark.asyncio
async def test_success_preserves_system_error_count_mid_retry() -> None:
    """065 Fix 18: when handle_system_error re-dispatches the same card+stage
    (system_error_last_at is set, notified is False), a successful dispatch must
    NOT zero the retry counter. Otherwise repeated post-dispatch failures
    (e.g. backend returning empty output) loop forever at attempt=1 instead of
    escalating to BLOCKED after _MAX_RETRIES.
    """
    from datetime import UTC, datetime

    svc = _Service()
    prior_last_at = datetime.now(UTC)
    state = _base_state(
        performer_services={"reviewing": svc},
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing"],
        system_error_count=2,
        system_error_reason="BACKEND_FORMAT_ERROR: review output was empty",
        system_error_last_at=prior_last_at,
        system_error_notified=False,
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert result["system_error_count"] == 2
    assert result["system_error_reason"] == "BACKEND_FORMAT_ERROR: review output was empty"
    assert result["system_error_last_at"] == prior_last_at
    assert result["system_error_notified"] is False


# ---------------------------------------------------------------------------
# 020 — Architecture plan injection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_plan_path_included_for_downstream_roles() -> None:
    """When the card has a plan_path, dispatch payload includes architecture_plan_path."""
    svc = _Service()
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )
    state["current_card"]["plan_path"] = "docs/coordinare-architecture.md"

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert svc.dispatched[0].get("architecture_plan_path") == "docs/coordinare-architecture.md"


@pytest.mark.asyncio
async def test_architecture_plan_path_not_included_for_architect() -> None:
    """When the role IS the architect, plan_path is NOT included."""
    svc = _Service()
    state = _base_state(
        performer_services={"architecting": svc},
        performer_stage="architecting",
        lifecycle_sequence=["architecting"],
    )
    state["current_card"]["plan_path"] = "docs/coordinare-architecture.md"

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert "architecture_plan_path" not in svc.dispatched[0]


@pytest.mark.asyncio
async def test_role_passed_in_dispatch_payload() -> None:
    """The performer_stage is passed as 'role' in the dispatch payload."""
    svc = _Service()
    state = _base_state(
        performer_services={"reviewing": svc},
        performer_stage="reviewing",
        lifecycle_sequence=["reviewing"],
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert svc.dispatched[0].get("role") == "reviewing"


@pytest.mark.asyncio
async def test_plan_path_empty_string_not_injected() -> None:
    """When the card has plan_path="" (empty string), architecture_plan_path
    must NOT appear in the dispatch payload — the truthiness guard in
    dispatch_performer should filter it out."""
    svc = _Service()
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )
    state["current_card"]["plan_path"] = ""

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert "architecture_plan_path" not in svc.dispatched[0]


# ---------------------------------------------------------------------------
# 033 — Smart Health-Check Retry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_retry_succeeds_on_third_attempt() -> None:
    """Health check fails twice then succeeds → dispatch proceeds."""
    from unittest.mock import AsyncMock, patch

    call_count = 0

    class _RetryService:
        def __init__(self) -> None:
            self.dispatched: list[dict] = []

        async def check_health(self) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("not ready")
            return {"status": "accepted"}

        async def dispatch_card(self, card_context, workspace_info=None):
            self.dispatched.append(card_context)
            return {"status": "accepted", "session_id": "s1"}

    from coordinare.config import HealthCheckConfig, ProjectConfiguration

    svc = _RetryService()
    config = ProjectConfiguration(
        project_name="Test", github_org="acme", github_project_number=1,
        github_token="tok", human_reviewers=["alice"],
        health_check=HealthCheckConfig(max_attempts=3, backoff_seconds=0.01),
    )

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    with patch("coordinare.graph.nodes.dispatch_performer.asyncio.sleep", new=AsyncMock()):
        result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(svc.dispatched) == 1
    assert call_count == 3


@pytest.mark.asyncio
async def test_health_retries_exhausted_goes_idle() -> None:
    """All retries exhausted → idle."""
    from unittest.mock import AsyncMock, patch

    class _AlwaysUnreachable:
        async def check_health(self) -> dict:
            raise ConnectionError("down")
        async def dispatch_card(self, *a, **kw):
            raise AssertionError("Should not dispatch")

    from coordinare.config import HealthCheckConfig, ProjectConfiguration

    svc = _AlwaysUnreachable()
    config = ProjectConfiguration(
        project_name="Test", github_org="acme", github_project_number=1,
        github_token="tok", human_reviewers=["alice"],
        health_check=HealthCheckConfig(max_attempts=2, backoff_seconds=0.01),
    )

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    with patch("coordinare.graph.nodes.dispatch_performer.asyncio.sleep", new=AsyncMock()):
        result = await dispatch_performer(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_health_error_not_retried() -> None:
    """Health status 'error' → blocked immediately, no retry."""

    class _ErrorService:
        async def check_health(self) -> dict:
            return {"status": "error", "reason": "crashed"}
        async def dispatch_card(self, *a, **kw):
            raise AssertionError("Should not dispatch")

    svc = _ErrorService()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )

    result = await dispatch_performer(state)

    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_health_single_attempt_no_retry() -> None:
    """max_attempts=1 → one attempt, no sleep."""
    from unittest.mock import AsyncMock, patch

    class _Unreachable:
        async def check_health(self) -> dict:
            raise ConnectionError("down")
        async def dispatch_card(self, *a, **kw):
            raise AssertionError("Should not dispatch")

    from coordinare.config import HealthCheckConfig, ProjectConfiguration

    svc = _Unreachable()
    config = ProjectConfiguration(
        project_name="Test", github_org="acme", github_project_number=1,
        github_token="tok", human_reviewers=["alice"],
        health_check=HealthCheckConfig(max_attempts=1, backoff_seconds=1.0),
    )

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        config=config,
    )

    sleep_mock = AsyncMock()
    with patch("coordinare.graph.nodes.dispatch_performer.asyncio.sleep", new=sleep_mock):
        result = await dispatch_performer(state)

    assert result["phase"] == "idle"
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_health_cancelled_error_propagates() -> None:
    """CancelledError during health check propagates immediately."""
    import asyncio as _aio

    class _CancelService:
        async def check_health(self) -> dict:
            raise _aio.CancelledError()
        async def dispatch_card(self, *a, **kw):
            raise AssertionError("Should not dispatch")

    svc = _CancelService()

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
    )

    with pytest.raises(_aio.CancelledError):
        await dispatch_performer(state)


# ---------------------------------------------------------------------------
# 031 — Human Override Controls in dispatch_performer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_performer_skip_override_continues_dispatching() -> None:
    """Skip override advances stage, then dispatch continues for the new stage."""
    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc, "reviewing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["pending_override"] = {"action": "skip"}

    result = await dispatch_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["pending_override"] is None
    # Skip override keeps phase=dispatching, so dispatch continues for reviewing
    assert result["phase"] == "monitoring_performer"
    assert len(svc.dispatched) == 1  # dispatched the reviewing stage


@pytest.mark.asyncio
async def test_dispatch_performer_veto_override_returns_early() -> None:
    """Veto override returns immediately without dispatching."""
    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["pending_override"] = {"action": "veto"}

    result = await dispatch_performer(state)

    assert result["phase"] == "blocked"
    assert result["pending_override"] is None
    assert len(svc.dispatched) == 0  # veto prevents dispatch


@pytest.mark.asyncio
async def test_dispatch_performer_skip_final_stage_moves_card() -> None:
    """Skip override on final stage moves card to IN_REVIEW via dispatch_performer."""
    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {
        "id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS",
        "pr_url": "https://github.com/test/1", "pr_node_id": "PR_1",
    }
    state["github_service"] = github
    state["pending_override"] = {"action": "skip"}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "IN_REVIEW") in github.move_calls


@pytest.mark.asyncio
async def test_dispatch_performer_skip_final_missing_pr_fields() -> None:
    """Skip override on final stage without PR fields → system_error."""
    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["pending_override"] = {"action": "skip"}

    result = await dispatch_performer(state)

    assert result["phase"] == "system_error"
    assert result["system_error_notified"] is False
    assert "pr_url" in result.get("system_error_reason", "")


# ---------------------------------------------------------------------------
# 034 — Cost & Token Tracking reset tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_resets_token_counters_on_first_role() -> None:
    """dispatch_performer resets token counters when dispatching the first role."""
    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["card_tokens_total"] = 5000
    state["card_cost_estimate"] = 15.0
    state["card_budget_alert_sent"] = True

    result = await dispatch_performer(state)

    assert result["card_tokens_total"] == 0
    assert result["card_cost_estimate"] == 0.0
    assert result["card_budget_alert_sent"] is False


# ---------------------------------------------------------------------------
# 037 — Per-Role Model Selection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_includes_backend_from_role_config() -> None:
    """dispatch_performer includes backend from PerformerRoleConfig in payload."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test",
        "github_org": "org",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="claude_code"),
        ),
    })

    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = github
    state["config"] = config

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    card_context = svc.dispatched[0]
    assert card_context.get("backend") == "claude_code"


@pytest.mark.asyncio
async def test_dispatch_includes_model_from_self_hosted_mode() -> None:
    """080: dispatch resolves model + base_url + bearer auth from a self-hosted mode."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test",
        "github_org": "org",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "endpoints": [
            {"name": "local-ollama", "kind": "litellm", "base_url": "https://proxy.internal/v1",
             "auth_env": "LITELLM_PROXY_KEY"},
        ],
        "model_endpoints": [
            {"name": "sonnet-local-ollama", "endpoint": "local-ollama", "model": "claude-sonnet-4-20250514"},
        ],
        "modes": [
            {"name": "single-sonnet", "strategy": "single", "tool": "sonnet-local-ollama"},
        ],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="claude_code", mode="single-sonnet"),
        ),
    })

    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = github
    state["config"] = config

    await dispatch_performer(state)

    card_context = svc.dispatched[0]
    assert card_context.get("backend") == "claude_code"
    assert card_context.get("model") == "claude-sonnet-4-20250514"
    assert card_context.get("base_url") == "https://proxy.internal/v1"
    # self-hosted endpoint → proxy bearer auth (auth_token_env), not api_key_env
    assert card_context.get("auth_token_env") == "LITELLM_PROXY_KEY"
    assert "api_key_env" not in card_context


@pytest.mark.asyncio
async def test_dispatch_includes_orchestration_block_for_multi_model_mode() -> None:
    """080: a non-single mode plumbs the orchestration block into card_context."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test", "github_org": "org", "github_project_number": 1,
        "github_token": "tok", "human_reviewers": ["alice"],
        "endpoints": [
            {"name": "local-ollama", "kind": "litellm", "base_url": "http://localhost:4000",
             "auth_env": "LITELLM_PROXY_AUTH_TOKEN"},
        ],
        "model_endpoints": [
            {"name": "gptoss", "endpoint": "local-ollama", "model": "local/gpt-oss:120b"},
            {"name": "qwen", "endpoint": "local-ollama", "model": "local/qwen3.6:35b"},
        ],
        "modes": [
            {"name": "always-x", "strategy": "always", "thinking": "gptoss", "tool": "qwen"},
        ],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="codex", mode="always-x"),
        ),
    })

    svc = _Service()
    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["config"] = config

    await dispatch_performer(state)

    orch = svc.dispatched[0].get("orchestration")
    assert orch is not None
    assert orch["strategy"] == "always"
    assert orch["thinking"]["model"] == "local/gpt-oss:120b"
    assert orch["tool"]["model"] == "local/qwen3.6:35b"


@pytest.mark.asyncio
async def test_dispatch_single_mode_has_no_orchestration_block() -> None:
    """080: single-strategy modes carry NO orchestration block (no proxy)."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test", "github_org": "org", "github_project_number": 1,
        "github_token": "tok", "human_reviewers": ["alice"],
        "endpoints": [{"name": "anthropic-cloud", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"}],
        "model_endpoints": [{"name": "sonnet", "endpoint": "anthropic-cloud", "model": "claude-sonnet-4-5"}],
        "modes": [{"name": "single-sonnet", "strategy": "single", "tool": "sonnet"}],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="claude_code", mode="single-sonnet"),
        ),
    })

    svc = _Service()
    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["config"] = config

    await dispatch_performer(state)

    assert "orchestration" not in svc.dispatched[0]


@pytest.mark.asyncio
async def test_dispatch_native_mode_sets_api_key_env_and_no_base_url() -> None:
    """080: a native (frontier) mode yields model + api_key_env and NO base_url override."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test",
        "github_org": "org",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "endpoints": [
            {"name": "anthropic-cloud", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"},
        ],
        "model_endpoints": [
            {"name": "sonnet-native", "endpoint": "anthropic-cloud", "model": "claude-sonnet-4-5"},
        ],
        "modes": [
            {"name": "native-sonnet", "strategy": "single", "tool": "sonnet-native"},
        ],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="claude_code", mode="native-sonnet"),
        ),
    })

    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = github
    state["config"] = config

    await dispatch_performer(state)

    card_context = svc.dispatched[0]
    assert card_context.get("model") == "claude-sonnet-4-5"
    assert card_context.get("api_key_env") == "ANTHROPIC_API_KEY"
    assert "base_url" not in card_context
    assert "auth_token_env" not in card_context


@pytest.mark.asyncio
async def test_dispatch_omits_base_url_and_api_key_env_when_unset() -> None:
    """dispatch_performer omits base_url/api_key_env when not configured."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test",
        "github_org": "org",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="claude_code"),
        ),
    })

    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = github
    state["config"] = config

    await dispatch_performer(state)

    card_context = svc.dispatched[0]
    assert "base_url" not in card_context
    assert "api_key_env" not in card_context


@pytest.mark.asyncio
async def test_dispatch_omits_model_when_not_set() -> None:
    """dispatch_performer omits model from payload when not configured."""
    from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

    config = ProjectConfiguration(**{
        "project_name": "test",
        "github_org": "org",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "performers": PerformersConfig(
            implementer=PerformerRoleConfig(backend="opencode"),
        ),
    })

    svc = _Service()
    github = _GitHub()

    state = initial_state()
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "title": "Test", "status": "TODO"}
    state["github_service"] = github
    state["config"] = config

    await dispatch_performer(state)

    card_context = svc.dispatched[0]
    assert card_context.get("backend") == "opencode"
    assert "model" not in card_context


# ---------------------------------------------------------------------------
# Env-cache volume injection (spec 060)
# ---------------------------------------------------------------------------


def _make_http_service(*, mode: str = "ephemeral", devenv_root: str = "/devenv") -> Any:
    """Return a MagicMock(spec=HTTPPerformerService) with minimal attrs set."""
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.services.http_performer_service import HTTPPerformerService

    svc = MagicMock(spec=HTTPPerformerService)
    svc.mode = mode
    svc.devenv_root = devenv_root
    svc.check_health = AsyncMock(return_value={"status": "accepted"})
    svc.dispatch_card = AsyncMock(return_value={"status": "accepted", "session_id": "sess-1"})
    return svc


def _ready_env_cache(tmp_path: Path, symphony_name: str = "my-project") -> dict:
    """Return an env_cache dict with one ready EnvCacheState."""
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "activate.sh").touch()
    state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        readme_sha="abc123",
        cache_dir_ready=True,
        # 077: a "ready" cache for consumer dispatch must be current + verified —
        # last bootstrap verified-succeeded and the cache reflects the current
        # spec (readme_sha == last_seen_spec_sha).
        last_bootstrap_succeeded=True,
        last_seen_spec_sha="abc123",
    )
    return {symphony_name: state}


@pytest.mark.asyncio
async def test_env_cache_volume_injected_for_ephemeral_http_service(tmp_path: Path) -> None:
    """Ephemeral HTTP service gets env-cache volume + env_cache_path in card context."""
    svc = _make_http_service(mode="ephemeral")
    symphony_name = "my-project"
    env_cache = _ready_env_cache(tmp_path, symphony_name)

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    assert svc.dispatch_card.called
    call_kwargs = svc.dispatch_card.call_args.kwargs
    extra_vols = call_kwargs.get("extra_volumes")
    assert extra_vols is not None and len(extra_vols) == 1
    assert extra_vols[0].mode == "ro"

    card_context = svc.dispatch_card.call_args.args[0]
    assert "env_cache_path" in card_context
    assert card_context["env_cache_path"].startswith("/devenv/")


@pytest.mark.asyncio
async def test_env_cache_consumer_dispatch_held_when_activate_missing(
    tmp_path: Path,
) -> None:
    """Consumer dispatch is held entirely when activate.sh hasn't been written.
    The card retries on the next pickup cycle once env_bootstrap completes.
    """
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    not_ready_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        cache_dir_ready=False,
    )
    env_cache = {symphony_name: not_ready_state}

    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    # Dispatch was held — performer was never invoked.
    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_env_cache_bootstrap_dispatch_proceeds_without_activate(
    tmp_path: Path,
) -> None:
    """The env_bootstrap stage itself is exempt from the activate.sh gate —
    that's the run that creates activate.sh in the first place.
    """
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    not_ready_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        cache_dir_ready=False,
    )
    env_cache = {symphony_name: not_ready_state}

    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        performer_services={"env_bootstrap": svc},
        performer_stage="env_bootstrap",
        lifecycle_sequence=["env_bootstrap"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    assert svc.dispatch_card.called
    call_kwargs = svc.dispatch_card.call_args.kwargs
    extra_vols = call_kwargs.get("extra_volumes")
    assert extra_vols is not None and len(extra_vols) == 1
    assert extra_vols[0].mode == "rw"


@pytest.mark.asyncio
async def test_serialize_env_bootstrap_holds_consumer_while_bootstrap_in_flight(
    tmp_path: Path,
) -> None:
    """When serialize_env_bootstrap=True, consumer dispatch is held while
    bootstrap_in_flight is True even if activate.sh already exists -- prevents
    overlap on a shared single-tenant LLM backend during bootstrap teardown.
    """
    from coordinare.config import ProjectConfiguration
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "activate.sh").touch()  # activate.sh exists -- only the new gate should hold
    ec_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        cache_dir_ready=True,
        bootstrap_in_flight=True,
    )
    env_cache = {symphony_name: ec_state}

    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        serialize_env_bootstrap=True,
    )
    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
        config=config,
    )

    await dispatch_performer(state)

    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_consumer_held_during_bootstrap_in_flight_regardless_of_serialize(
    tmp_path: Path,
) -> None:
    """077: a consumer must NOT dispatch while a bootstrap is in flight, even with
    serialize_env_bootstrap=False (the flag is now moot — the strict current+
    verified gate always holds during an in-flight bootstrap). Previously the
    default let consumers race the bootstrap against a stale cache.
    """
    from coordinare.config import ProjectConfiguration
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "activate.sh").touch()
    ec_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        readme_sha="abc123",
        last_seen_spec_sha="abc123",
        last_bootstrap_succeeded=True,
        cache_dir_ready=True,
        bootstrap_in_flight=True,  # a re-bootstrap is mid-run
    )
    env_cache = {symphony_name: ec_state}

    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        serialize_env_bootstrap=False,
    )
    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
        config=config,
    )

    await dispatch_performer(state)

    # Held: in-flight bootstrap blocks the consumer even with serialize off.
    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_consumer_held_when_cache_predates_current_spec(tmp_path: Path) -> None:
    """077: even with a verified, ready, not-in-flight cache, a consumer is held
    when the cache reflects an OLD spec (readme_sha != last_seen_spec_sha) — e.g.
    the README just changed to add a dependency. Closes the stale-cache window
    between a spec change and the re-bootstrap that satisfies it.
    """
    from coordinare.config import ProjectConfiguration
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "activate.sh").touch()
    ec_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        readme_sha="OLD",          # cache was built for the old spec
        last_seen_spec_sha="NEW",  # current spec has since changed
        last_bootstrap_succeeded=True,
        cache_dir_ready=True,
        bootstrap_in_flight=False,
    )
    config = ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
    )
    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache={symphony_name: ec_state},
        config=config,
    )

    await dispatch_performer(state)

    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_env_cache_persistent_http_service_warns_and_skips_volumes(
    tmp_path: Path,
) -> None:
    """Persistent HTTP service: warning logged, no volumes passed (Docker can't hot-add mounts)."""
    svc = _make_http_service(mode="persistent")
    symphony_name = "my-project"
    env_cache = _ready_env_cache(tmp_path, symphony_name)

    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    assert svc.dispatch_card.called
    call_kwargs = svc.dispatch_card.call_args.kwargs
    # Volumes are NOT passed — Docker cannot hot-add mounts to a running container.
    assert call_kwargs.get("extra_volumes") is None
    card_context = svc.dispatch_card.call_args.args[0]
    assert "env_cache_path" not in card_context


@pytest.mark.asyncio
async def test_env_cache_persistent_http_service_held_when_activate_missing(
    tmp_path: Path,
) -> None:
    """Persistent HTTP service consumer dispatch is also held when activate.sh
    is missing — the dispatch gate runs before the persistent/ephemeral split.
    """
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import sanitise_symphony_name

    symphony_name = "my-project"
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    not_ready = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        cache_dir_ready=False,
    )
    env_cache = {symphony_name: not_ready}

    svc = _make_http_service(mode="persistent")
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_env_cache_non_http_service_no_extra_volumes(tmp_path: Path) -> None:
    """Non-HTTP service (_Service) gets env_cache_path in card context but no extra_volumes kwarg."""
    symphony_name = "my-project"
    env_cache = _ready_env_cache(tmp_path, symphony_name)

    svc = _Service()
    state = _base_state(
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    # _Service.dispatch_card doesn't accept extra_volumes and must not be called with it
    # (the node guards with isinstance(service, HTTPPerformerService) before passing extra_volumes)
    assert svc.last_workspace_info is not None  # workspace was still prepared


# ---------------------------------------------------------------------------
# 083 US1: security-role static-analysis floor at dispatch (T010)
# ---------------------------------------------------------------------------


class _GitHubWithDiff(_GitHub):
    """_GitHub plus the 083 ``get_pr_diff`` contract."""

    def __init__(
        self,
        *,
        diff_files: list[str] | None = None,
        diff_raw: str = "diff --git a/x b/x",
        raise_exc: Exception | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._diff_files = diff_files if diff_files is not None else ["src/app/vuln.py"]
        self._diff_raw = diff_raw
        self._raise_exc = raise_exc
        self.get_pr_diff_calls: list[str] = []

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        self.get_pr_diff_calls.append(pr_url)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._diff_raw, list(self._diff_files)


_SEC_CARD = {
    "id": "ITEM_1",
    "status": "TODO",
    "pr_url": "https://github.com/acme/repo/pull/42",
    "pr_node_id": "PR_node_1",
}

_SAMPLE_FINDINGS = [
    {
        "severity": "high",
        "category": "78",
        "description": "semgrep:dangerous-system-call",
        "file": "src/app/vuln.py",
        "line": 3,
        "routing": "implementer",
    }
]


@pytest.mark.asyncio
async def test_security_role_scans_once_and_stashes_findings(monkeypatch) -> None:
    """security role → get_pr_diff once + scan_diff once + findings stashed/injected."""
    security_svc = _Service()
    github = _GitHubWithDiff()
    state = _base_state(
        github_service=github,
        performer_services={"security": security_svc},
        performer_stage="security",
        lifecycle_sequence=["security"],
        current_card=dict(_SEC_CARD),
    )

    calls: list[tuple[list[str], str]] = []

    def fake_scan(changed_files, repo_root):
        calls.append((list(changed_files), str(repo_root)))
        return list(_SAMPLE_FINDINGS)

    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.scan_diff", fake_scan
    )

    result = await dispatch_performer(state)

    assert github.get_pr_diff_calls == ["https://github.com/acme/repo/pull/42"]
    assert len(calls) == 1
    assert calls[0][0] == ["src/app/vuln.py"]
    assert result["scanner_findings"] == _SAMPLE_FINDINGS
    assert security_svc.dispatched[0]["scanner_findings"] == _SAMPLE_FINDINGS


@pytest.mark.asyncio
async def test_non_security_role_does_not_scan(monkeypatch) -> None:
    """Non-security role → scan_diff NOT called, no scanner_findings state key."""
    impl_svc = _Service()
    github = _GitHubWithDiff()
    state = _base_state(
        github_service=github,
        performer_services={"implementing": impl_svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_card=dict(_SEC_CARD),
    )

    called: list[int] = []

    def fake_scan(*a, **k):
        called.append(1)
        return []

    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.scan_diff", fake_scan
    )

    result = await dispatch_performer(state)

    assert called == []
    assert "scanner_findings" not in result
    assert "scanner_findings" not in impl_svc.dispatched[0]


@pytest.mark.asyncio
async def test_scan_error_fails_closed_with_synthetic_finding(monkeypatch) -> None:
    """scan_diff raises ScannerError → fail-closed synthetic critical finding."""
    from coordinare.services.security_scanner import ScannerError

    security_svc = _Service()
    github = _GitHubWithDiff()
    state = _base_state(
        github_service=github,
        performer_services={"security": security_svc},
        performer_stage="security",
        lifecycle_sequence=["security"],
        current_card=dict(_SEC_CARD),
    )

    def boom(*a, **k):
        raise ScannerError("semgrep binary not found")

    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.scan_diff", boom
    )

    result = await dispatch_performer(state)

    findings = result["scanner_findings"]
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "critical"
    assert f["category"] == "scanner_unavailable"
    assert f["routing"] == "halt"
    assert security_svc.dispatched[0]["scanner_findings"] == findings


@pytest.mark.asyncio
async def test_diff_fetch_error_fails_closed(monkeypatch) -> None:
    """get_pr_diff raises → fail-closed synthetic finding; scan_diff never reached."""
    security_svc = _Service()
    github = _GitHubWithDiff(raise_exc=RuntimeError("fetch failed (status 503)"))
    state = _base_state(
        github_service=github,
        performer_services={"security": security_svc},
        performer_stage="security",
        lifecycle_sequence=["security"],
        current_card=dict(_SEC_CARD),
    )

    def must_not_scan(*a, **k):
        raise AssertionError("scan_diff must not run when diff fetch fails")

    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.scan_diff", must_not_scan
    )

    result = await dispatch_performer(state)

    findings = result["scanner_findings"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert findings[0]["category"] == "scanner_unavailable"
    assert findings[0]["routing"] == "halt"


# ---------------------------------------------------------------------------
# Reviewer-diff injection: review roles receive the PR diff inline so a model
# that does not proactively fetch it cannot report "no code changes".
# (Drive-by fix bundled on 083: bot rejected PR #154 with "no code changes".)
# ---------------------------------------------------------------------------

_REVIEW_CARD = {
    "id": "ITEM_REVIEW",
    "status": "TODO",
    "pr_url": "https://github.com/acme/repo/pull/42",
    "pr_node_id": "PR_node_1",
}

_REVIEW_DIFF = "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "role_key"),
    [
        ("reviewing", "reviewing"),
        ("closing_review", "closing_review"),
        ("qa", "qa"),
        # documenting is covered separately (124: it always dispatches — see
        # test_documenting_dispatched_even_without_doc_changes); it does not fit
        # this review-diff-injection parametrization.
    ],
)
async def test_review_roles_receive_pr_diff(stage, role_key) -> None:
    """Review roles get the raw PR diff injected as card_context['pr_diff']."""
    svc = _Service()
    github = _GitHubWithDiff(diff_raw=_REVIEW_DIFF, diff_files=["src/app.py"])
    state = _base_state(
        github_service=github,
        performer_services={role_key: svc},
        performer_stage=stage,
        lifecycle_sequence=[stage],
        current_card=dict(_REVIEW_CARD),
    )

    await dispatch_performer(state)

    assert github.get_pr_diff_calls == ["https://github.com/acme/repo/pull/42"]
    assert svc.dispatched[0].get("pr_diff") == _REVIEW_DIFF


@pytest.mark.asyncio
async def test_implementing_role_does_not_receive_pr_diff() -> None:
    """The implementer is not a review role → no pr_diff fetch/injection."""
    svc = _Service()
    github = _GitHubWithDiff(diff_raw=_REVIEW_DIFF)
    state = _base_state(
        github_service=github,
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_card=dict(_REVIEW_CARD),
    )

    await dispatch_performer(state)

    assert github.get_pr_diff_calls == []
    assert "pr_diff" not in svc.dispatched[0]


@pytest.mark.asyncio
async def test_review_role_diff_fetch_failure_is_graceful() -> None:
    """A diff-fetch failure does NOT block dispatch and injects no pr_diff key.

    The persona-hardening fallback (instructing the model to fetch the diff
    itself) covers this path, so the dispatch must proceed normally.
    """
    svc = _Service()
    github = _GitHubWithDiff(raise_exc=RuntimeError("fetch failed (status 503)"))
    state = _base_state(
        github_service=github,
        performer_services={"reviewing": svc},
        performer_stage="reviewing",
        lifecycle_sequence=["reviewing"],
        current_card=dict(_REVIEW_CARD),
    )

    await dispatch_performer(state)

    # dispatch still happened, just without an injected diff
    assert len(svc.dispatched) == 1
    assert "pr_diff" not in svc.dispatched[0]


@pytest.mark.asyncio
async def test_review_role_empty_diff_not_injected() -> None:
    """An empty diff string is not injected (no misleading empty 'pr_diff')."""
    svc = _Service()
    github = _GitHubWithDiff(diff_raw="", diff_files=[])
    state = _base_state(
        github_service=github,
        performer_services={"reviewing": svc},
        performer_stage="reviewing",
        lifecycle_sequence=["reviewing"],
        current_card=dict(_REVIEW_CARD),
    )

    await dispatch_performer(state)

    assert "pr_diff" not in svc.dispatched[0]


# ---------------------------------------------------------------------------
# 092 US2: dispatch-level test-env wiring regression coverage
#
# The integration suite only exercises resolve_test_env_vars() in ISOLATION via
# explicit kwargs — it never drives the dispatch_performer wiring that derives
# github_org / repo from the effective ProjectConfiguration. That coverage gap
# let a `.global_config` typo (state["config"] is already a ProjectConfiguration,
# not a CoordinareConfiguration wrapper) ship and crash live consumer dispatch
# with AttributeError after the env-cache built. These tests drive the real path.
# ---------------------------------------------------------------------------


class _GitHubWithFileContent(_GitHub):
    """_GitHub plus a recording get_file_content (for the test-env fallback flow)."""

    def __init__(self, *, file_content: str | None = None) -> None:
        super().__init__()
        self._file_content = file_content
        self.get_file_content_calls: list[tuple[str, str, str]] = []

    async def get_file_content(self, org: str, repo: str, path: str) -> str | None:
        self.get_file_content_calls.append((org, repo, path))
        return self._file_content


def _ready_env_cache_with_test_env_source(
    tmp_path: Path, symphony_name: str, source: str
) -> dict:
    """A ready env_cache whose state carries a persisted agent-discovered test_env_source."""
    env_cache = _ready_env_cache(tmp_path, symphony_name)
    env_cache[symphony_name].test_env_source = source
    return env_cache


@pytest.mark.asyncio
async def test_dispatch_resolves_test_env_via_effective_config(tmp_path: Path) -> None:
    """Consumer dispatch derives github_org/repo from the SymphonyConfig's
    effective_config (a ProjectConfiguration) and injects the resolved vars into
    card_context["test_env_vars"] — keys only reach logs, values ride secrets.

    Regression: state["config"] is the ProjectConfiguration itself; passing
    `.global_config` to effective_config() raised AttributeError and crashed
    dispatch after the env-cache built (092 live failure).
    """
    from coordinare.config import ProjectConfiguration, SymphonyConfig

    symphony_name = "my-project"
    github = _GitHubWithFileContent(file_content="POSTGRESQL_PASSWORD=\nFOO=bar\n")
    env_cache = _ready_env_cache_with_test_env_source(
        tmp_path, symphony_name, source=".env.test"
    )
    global_cfg = ProjectConfiguration(
        github_org="acme-org",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
    )
    sym_cfg = SymphonyConfig(name=symphony_name, github_project_number=1)

    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        github_service=github,
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
        config=global_cfg,
        symphony_configs={symphony_name: sym_cfg},
    )

    # Must not raise AttributeError ('...has no attribute global_config').
    await dispatch_performer(state)

    # github_org came from effective_config; repo defaulted to the symphony name.
    assert github.get_file_content_calls == [("acme-org", symphony_name, ".env.test")]

    assert svc.dispatch_card.called
    card_context = svc.dispatch_card.call_args.args[0]
    assert card_context["test_env_vars"] == {"POSTGRESQL_PASSWORD": "", "FOO": "bar"}


@pytest.mark.asyncio
async def test_dispatch_test_env_absent_when_no_source_configured(
    tmp_path: Path,
) -> None:
    """No test_env block and no persisted fallback source → no test_env_vars key
    (and resolve_test_env_vars never reaches the GitHub API)."""
    from coordinare.config import ProjectConfiguration, SymphonyConfig

    symphony_name = "my-project"
    github = _GitHubWithFileContent(file_content="SHOULD_NOT_BE_READ=1\n")
    env_cache = _ready_env_cache(tmp_path, symphony_name)  # no test_env_source
    global_cfg = ProjectConfiguration(
        github_org="acme-org",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
    )
    sym_cfg = SymphonyConfig(name=symphony_name, github_project_number=1)

    svc = _make_http_service(mode="ephemeral")
    state = _base_state(
        github_service=github,
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        current_symphony=symphony_name,
        env_cache=env_cache,
        config=global_cfg,
        symphony_configs={symphony_name: sym_cfg},
    )

    await dispatch_performer(state)

    assert github.get_file_content_calls == []
    assert svc.dispatch_card.called
    card_context = svc.dispatch_card.call_args.args[0]
    assert "test_env_vars" not in card_context


# ---------------------------------------------------------------------------
# 124 (FR-006): the wiki-maintaining documenter always runs (no docs-path skip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_documenting_skipped_without_doc_changes() -> None:
    """125 (restores conditional documenting): with no prior doc pass and a PR
    touching no docs/ path, the documenting stage is skipped via the SHA-keyed
    gate's whole-PR fallback (_should_skip_documenting).

    NOTE: this REVERSES 124 FR-006 ("documenter always runs for the living
    wiki"). Restoring the gate was an explicit product decision (see the
    dispatch_performer caveat comment); it needs 124-owner sign-off, and the
    docs/-only skip predicate may under-run 124's code-derived wiki."""
    svc = _Service()
    github = _GitHubWithDiff(diff_raw=_REVIEW_DIFF, diff_files=["src/app.py"])  # no docs/ path
    state = _base_state(
        github_service=github,
        performer_services={"documenting": svc, "closing_review": _Service()},
        performer_stage="documenting",
        lifecycle_sequence=["documenting", "closing_review"],
        current_card=dict(_REVIEW_CARD),
    )

    await dispatch_performer(state)

    assert svc.dispatched == []
    assert state["performer_stage"] == "closing_review"


# ---------------------------------------------------------------------------
# 123 US4 (T014): prior_clarifications injected on assessor re-dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prior_clarifications_injected_when_present() -> None:
    """123 FR-011: assessor re-dispatch carries persisted Q&A as
    prior_clarifications."""
    svc = _Service()
    qa = [{"question": "Should auth use OAuth?", "answer": "Yes, OAuth2 PKCE"}]
    state = _base_state(
        performer_services={"assessing": svc},
        performer_stage="assessing",
        lifecycle_sequence=["assessing"],
        assessor_open_questions=qa,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert svc.dispatched[0].get("prior_clarifications") == qa


@pytest.mark.asyncio
async def test_prior_clarifications_absent_on_first_dispatch() -> None:
    """123 FR-011: first assessor dispatch (no prior Q&A) omits the key."""
    svc = _Service()
    state = _base_state(
        performer_services={"assessing": svc},
        performer_stage="assessing",
        lifecycle_sequence=["assessing"],
        assessor_open_questions=[],
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert "prior_clarifications" not in svc.dispatched[0]


# ---------------------------------------------------------------------------
# 123 fix: PR-diff sanitizer — filter agent/vendor/binary noise + cap size
# (prevents committed .codex/ junk from overflowing the model context → 400)
# ---------------------------------------------------------------------------

from coordinare.graph.nodes.dispatch_performer import _sanitize_pr_diff  # noqa: E402


def _diff(path: str, body: str = "+x\n") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_sanitize_drops_agent_tooling_sections():
    raw = _diff(".codex/.tmp/plugins") + _diff("app/views/about.html.erb", "+<h1>About</h1>\n") + _diff("node_modules/x/index.js")
    out = _sanitize_pr_diff(raw)
    assert ".codex/" not in out
    assert "node_modules/" not in out
    assert "app/views/about.html.erb" in out  # the real change is kept
    assert "[coordinare:" in out and "tooling/vendor" in out


def test_sanitize_drops_binary_sections():
    raw = _diff("app/models/user.rb", "+# real\n") + (
        "diff --git a/app/assets/logo.png b/app/assets/logo.png\n"
        "new file mode 100644\nBinary files /dev/null and b/app/assets/logo.png differ\n"
    )
    out = _sanitize_pr_diff(raw)
    assert "user.rb" in out
    assert "logo.png" not in out
    assert "binary file section" in out


def test_sanitize_caps_oversized_diff_and_notes_truncation():
    big = _diff("app/huge.rb", "+" + ("x" * 200_000) + "\n")
    out = _sanitize_pr_diff(big, max_chars=10_000)
    assert len(out) <= 10_000 + 200  # cap + short note
    assert "truncated" in out


def test_sanitize_keeps_clean_diff_unchanged_no_note():
    raw = _diff("app/views/about.html.erb", "+<h1>About</h1>\n")
    out = _sanitize_pr_diff(raw)
    assert "about.html.erb" in out
    assert "[coordinare:" not in out  # nothing omitted/truncated → no note
