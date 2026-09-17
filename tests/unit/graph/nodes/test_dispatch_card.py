from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.state import initial_state
from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceInfo, WorkspaceSetupError

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _GitHub:
    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _Agent:
    def __init__(self) -> None:
        self.last_workspace_info: WorkspaceInfo | None = None

    async def check_health(self):
        return {"status": "accepted"}

    async def dispatch_card(self, card_context, workspace_info=None):
        self.last_workspace_info = workspace_info
        return {"status": "accepted", "session_id": "s1"}


class _AgentUnhealthy:
    async def check_health(self):
        return {"status": "error", "reason": "Agent is down"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise AssertionError("Should not dispatch when unhealthy")


class _AgentUnreachable:
    async def check_health(self):
        raise ConnectionError("Agent unreachable")

    async def dispatch_card(self, card_context, workspace_info=None):
        raise AssertionError("Should not dispatch when unreachable")


class _AgentTransportError:
    async def check_health(self):
        return {"status": "healthy"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise TransportError("Agent transport permanently failed")


class _AgentPermanentError:
    async def check_health(self):
        return {"status": "healthy"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise PermanentGitHubError("Token revoked")


class _WorkspaceManager:
    """Stub WorkspaceManager for unit tests."""

    def __init__(
        self,
        *,
        workspace_path: Path = Path("/tmp/fake-ws/repo"),
        raise_on_prepare: WorkspaceSetupError | None = None,
    ) -> None:
        self._path = workspace_path
        self._raise = raise_on_prepare
        self.prepare_calls: list[dict] = []
        self.teardown_calls: list[Path] = []

    async def prepare(self, card: dict) -> WorkspaceInfo:
        self.prepare_calls.append(card)
        if self._raise is not None:
            raise self._raise
        return WorkspaceInfo(
            path=self._path,
            branch="coordinare/ITEM_1/test-card",
            repo_url="https://github.com/acme/repo.git",
            github_token="test-token",
        )

    async def teardown(self, path: Path) -> None:
        self.teardown_calls.append(path)


# ---------------------------------------------------------------------------
# Helper: set up performer_services for 019 lifecycle (dispatch_card now
# delegates to dispatch_performer which reads performer_services)
# ---------------------------------------------------------------------------


def _add_performer_services(state, agent):
    """Register the mock agent as the implementer in performer_services."""
    state["performer_services"] = {"implementing": agent}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]


# ---------------------------------------------------------------------------
# Existing tests — all inject a no-op workspace_manager for backward compat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_moves_and_dispatches() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    agent = _Agent()
    state["agent_service"] = agent
    state["workspace_manager"] = _WorkspaceManager()
    _add_performer_services(state, agent)

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"]["status"] == "accepted"
    assert result["agent_health_status"] == "accepted"


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_unhealthy_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnhealthy()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "error"
    assert any("health check failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_card_stays_idle_on_unreachable_agent() -> None:
    """Exception during health check (unreachable) → idle for retry, not blocked."""
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnreachable()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "idle"
    assert result["agent_health_status"] == "unreachable"


@pytest.mark.asyncio
async def test_dispatch_card_transport_error_routes_to_system_error() -> None:
    """TransportError during dispatch → system_error phase for retry (not immediate BLOCKED)."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentTransportError()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert result["system_error_reason"] is not None
    # Card was moved to IN_PROGRESS before the transport error; not moved to BLOCKED
    assert ("ITEM_1", "IN_PROGRESS") in gh.move_calls
    assert ("ITEM_1", "BLOCKED") not in gh.move_calls


# ---------------------------------------------------------------------------
# New workspace tests (T012)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_enriches_payload_with_workspace_fields() -> None:
    """Agent receives WorkspaceInfo with correct fields when workspace_manager is set."""
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["workspace_manager"] = _WorkspaceManager(
        workspace_path=Path("/tmp/fake-ws/repo"),
    )

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert agent.last_workspace_info is not None
    assert agent.last_workspace_info.branch == "coordinare/ITEM_1/test-card"
    assert agent.last_workspace_info.repo_url == "https://github.com/acme/repo.git"
    assert agent.last_workspace_info.path == Path("/tmp/fake-ws/repo")

    # workspace_path stored in state
    assert result["workspace_path"] == Path("/tmp/fake-ws/repo")
    assert result["workspace_branch"] == "coordinare/ITEM_1/test-card"


@pytest.mark.asyncio
async def test_dispatch_blocks_card_on_workspace_setup_error() -> None:
    """WorkspaceSetupError → phase='blocked'; workspace_path not set in state."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _Agent()
    state["workspace_manager"] = _WorkspaceManager(
        raise_on_prepare=WorkspaceSetupError("git clone failed: connection refused"),
    )

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result.get("workspace_path") is None
    assert any("git clone failed" in q for q in result["open_questions"])
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


@pytest.mark.asyncio
async def test_dispatch_workspace_setup_error_move_card_fails_gracefully() -> None:
    """If move_card itself fails during WorkspaceSetupError handling, phase is still 'blocked'."""
    class _GitHubRaises:
        async def move_card(self, item_id: str, status: str) -> None:
            raise RuntimeError("GitHub API unavailable")

    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHubRaises()
    state["agent_service"] = _Agent()
    state["workspace_manager"] = _WorkspaceManager(
        raise_on_prepare=WorkspaceSetupError("disk full"),
    )

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert any("disk full" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_permanent_github_error_blocks_card() -> None:
    """PermanentGitHubError during dispatch → card moved to BLOCKED immediately."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentPermanentError()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert any("Token revoked" in q for q in result["open_questions"])
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


@pytest.mark.asyncio
async def test_dispatch_permanent_github_error_move_card_fails_gracefully() -> None:
    """If move_card(BLOCKED) fails during PermanentGitHubError handling, phase is still 'blocked'."""
    class _GitHubFailsOnBlocked:
        async def move_card(self, item_id: str, status: str) -> None:
            if status == "BLOCKED":
                raise RuntimeError("GitHub API unavailable")

    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHubFailsOnBlocked()
    state["agent_service"] = _AgentPermanentError()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert any("Token revoked" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_transport_error_tears_down_workspace() -> None:
    """TransportError during dispatch tears down the prepared workspace and clears state fields."""
    wm = _WorkspaceManager(workspace_path=Path("/tmp/fake-ws/repo"))
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentTransportError()
    state["workspace_manager"] = wm

    result = await dispatch_card(state)

    assert result["phase"] == "system_error"
    # Workspace was torn down
    assert wm.teardown_calls == [Path("/tmp/fake-ws/repo")]
    # State fields cleared so retries don't assume a live workspace
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


@pytest.mark.asyncio
async def test_dispatch_no_workspace_manager_still_dispatches() -> None:
    """When workspace_manager is None (backward compat), dispatch proceeds without workspace."""
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    # workspace_manager stays None (not set)

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert agent.last_workspace_info is None


# ---------------------------------------------------------------------------
# US4: workspace validation - T012-T015, T023
# ---------------------------------------------------------------------------


class _AgentNeverDispatched:
    """Raises if dispatch_card is called — verifies performer is never started."""

    async def check_health(self):
        return {"status": "accepted"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise AssertionError("dispatch_card must not be called when workspace is invalid")


class _WMRaisesRuntimeError:
    async def prepare(self, card):
        raise RuntimeError("Unexpected git internal error")

    async def teardown(self, path):
        pass


class _WMEmptyToken:
    async def prepare(self, card):
        return WorkspaceInfo(
            path=Path("/tmp/ws"),
            branch="coordinare/ITEM_1/test",
            repo_url="https://github.com/org/repo.git",
            github_token="",
        )

    async def teardown(self, path):
        pass


class _WMEmptyRepoUrl:
    async def prepare(self, card):
        return WorkspaceInfo(
            path=Path("/tmp/ws"),
            branch="coordinare/ITEM_1/test",
            repo_url="",
            github_token="tok",
        )

    async def teardown(self, path):
        pass


class _WMComplete:
    async def prepare(self, card):
        return WorkspaceInfo(
            path=Path("/tmp/ws"),
            branch="coordinare/ITEM_1/test",
            repo_url="https://github.com/org/repo.git",
            github_token="tok",
        )

    async def teardown(self, path):
        pass


class _WMKubernetes:
    """Simulates Kubernetes transport: path=None, github_token="" (auth via K8s Secrets)."""

    async def prepare(self, card):
        return WorkspaceInfo(
            path=None,
            branch="coordinare/ITEM_1/test",
            repo_url="https://github.com/org/repo.git",
            github_token="",
        )

    async def teardown(self, path):
        pass


@pytest.mark.asyncio
async def test_dispatch_card_workspace_exception_blocks_card() -> None:
    """Unexpected exception from workspace prepare() → card blocked, performer not started."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentNeverDispatched()
    state["workspace_manager"] = _WMRaisesRuntimeError()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in gh.move_calls
    assert result["open_questions"]


@pytest.mark.asyncio
async def test_dispatch_card_workspace_empty_github_token_blocks_card() -> None:
    """WorkspaceInfo with empty github_token → card blocked with reason mentioning github_token."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentNeverDispatched()
    state["workspace_manager"] = _WMEmptyToken()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in gh.move_calls
    assert any("github_token" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_card_workspace_empty_repo_url_blocks_card() -> None:
    """WorkspaceInfo with empty repo_url → card blocked."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentNeverDispatched()
    state["workspace_manager"] = _WMEmptyRepoUrl()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


@pytest.mark.asyncio
async def test_dispatch_card_workspace_complete_info_dispatches_normally() -> None:
    """WorkspaceInfo with all required fields populated → dispatch proceeds normally."""
    gh = _GitHub()
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = agent
    state["workspace_manager"] = _WMComplete()

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert agent.last_workspace_info is not None


@pytest.mark.asyncio
async def test_dispatch_card_workspace_manager_none_dispatches_without_workspace_context() -> None:
    """workspace_manager=None → dispatch proceeds normally (SSH/Kubernetes handle own workspace)."""
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["workspace_manager"] = None

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert agent.last_workspace_info is None


@pytest.mark.asyncio
async def test_dispatch_card_kubernetes_empty_token_dispatches_normally() -> None:
    """Kubernetes transport (path=None, github_token='') must NOT be blocked by completeness guard.

    The Kubernetes performer container injects auth via K8s Secrets; the coordinare
    workspace manager legitimately returns github_token='' for this transport.
    """
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["workspace_manager"] = _WMKubernetes()

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_performer"
    assert agent.last_workspace_info is not None
    assert agent.last_workspace_info.path is None
    assert agent.last_workspace_info.github_token == ""


# ---------------------------------------------------------------------------
# Copilot review fixes - workspace teardown on completeness failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_incomplete_workspace_tears_down_and_clears_state() -> None:
    """When workspace fields are incomplete, the workspace is torn down and state is cleared."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentNeverDispatched()
    state["workspace_manager"] = _WMEmptyToken()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


@pytest.mark.asyncio
async def test_dispatch_incomplete_workspace_teardown_failure_does_not_raise() -> None:
    """If teardown raises during incomplete workspace handling, phase is still 'blocked'."""
    class _WMEmptyTokenTeardownFails:
        async def prepare(self, card):
            from coordinare.workspace import WorkspaceInfo
            return WorkspaceInfo(
                path=Path("/tmp/ws"),
                branch="coordinare/ITEM_1/test",
                repo_url="https://github.com/org/repo.git",
                github_token="",
            )

        async def teardown(self, path):
            raise RuntimeError("Teardown disk error")

    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentNeverDispatched()
    state["workspace_manager"] = _WMEmptyTokenTeardownFails()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


# ---------------------------------------------------------------------------
# 018 — Persona injection tests (T008)
# ---------------------------------------------------------------------------


class _AgentCapture:
    """Captures the card_context dict passed to dispatch_card."""

    def __init__(self) -> None:
        self.captured_context: dict | None = None

    async def check_health(self):
        return {"status": "accepted"}

    async def dispatch_card(self, card_context, workspace_info=None):
        self.captured_context = card_context
        return {"status": "accepted", "session_id": "s1"}


def _make_config_with_persona(instructions: str):
    from coordinare.config import PersonaConfig, PersonasConfig
    personas = PersonasConfig(implementer=PersonaConfig(instructions=instructions))

    class _FakeConfig:
        pass

    cfg = _FakeConfig()
    cfg.personas = personas
    return cfg


@pytest.mark.asyncio
async def test_persona_instructions_present_in_payload_when_configured() -> None:
    """FR-004: custom persona instructions appear in dispatch payload."""
    agent = _AgentCapture()
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["config"] = _make_config_with_persona("Always write tests first.")

    await dispatch_card(state)

    assert agent.captured_context is not None
    assert agent.captured_context.get("persona_instructions") == "Always write tests first." + (
        "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
        "in English. Preserve code, paths, quoted source text, and machine-readable "
        "protocol fields exactly; do not translate those values."
    )


@pytest.mark.asyncio
async def test_persona_instructions_equals_default_when_config_field_empty() -> None:
    """FR-007: empty instructions → default (non-empty) instructions injected."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS
    agent = _AgentCapture()
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["config"] = _make_config_with_persona("")

    await dispatch_card(state)

    assert agent.captured_context is not None
    instructions = agent.captured_context.get("persona_instructions")
    assert instructions == DEFAULT_INSTRUCTIONS["implementer"] + (
        "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
        "in English. Preserve code, paths, quoted source text, and machine-readable "
        "protocol fields exactly; do not translate those values."
    )
    assert instructions  # non-empty


@pytest.mark.asyncio
async def test_persona_instructions_equals_default_when_whitespace_only() -> None:
    """FR-007: whitespace-only instructions → default instructions injected."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS
    agent = _AgentCapture()
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["config"] = _make_config_with_persona("   \n  ")

    await dispatch_card(state)

    assert agent.captured_context is not None
    instructions = agent.captured_context.get("persona_instructions")
    assert instructions == DEFAULT_INSTRUCTIONS["implementer"] + (
        "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
        "in English. Preserve code, paths, quoted source text, and machine-readable "
        "protocol fields exactly; do not translate those values."
    )
