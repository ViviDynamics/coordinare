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
    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


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
    assert card_ctx["persona_instructions"] == "Always write unit tests first."


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
    assert card_ctx["persona_instructions"] == DEFAULT_INSTRUCTIONS["implementer"]


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
    assert card_ctx["persona_instructions"] == "Be thorough in reviews."


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
    """On successful dispatch, system_error_count and related fields reset."""
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
