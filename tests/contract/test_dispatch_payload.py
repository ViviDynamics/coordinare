"""Contract tests: dispatch payload survives coordinare → performer boundary.

These tests verify that every field added to card_context by dispatch_performer
actually arrives in the performer's Score model after passing through
AgentService.dispatch_card() and the wire protocol.

If a test fails here, it means a field is being silently dropped — the most
dangerous class of bug in the coordinare-performer contract.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.protocol import ProtocolResponse
from coordinare.services.agent_service import AgentService


class _CaptureTransport:
    """Transport that captures the serialized payload without sending it."""

    def __init__(self) -> None:
        self.captured_payload: dict[str, Any] = {}

    async def send(self, message: Any) -> ProtocolResponse:
        self.captured_payload = dict(message.payload)
        return ProtocolResponse(status="accepted", session_id="test-session")

    async def close(self) -> None:
        pass


def _full_card_context() -> dict[str, Any]:
    """Build a card_context with ALL fields that dispatch_performer might set."""
    return {
        # Card identity (from check_board)
        "id": "PVTI_test123",
        "title": "Implement breadcrumbs",
        "description": "Add breadcrumb navigation to all pages",
        "acceptance_criteria": ["Breadcrumbs show on every page", "Tests pass"],
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "issue_id": "ISS_test123",
        "issue_number": 42,
        "issue_url": "https://github.com/org/repo/issues/42",
        # Performer lifecycle (from dispatch_performer)
        "role": "reviewing",
        "persona_instructions": "Focus on test coverage and code quality.",
        "relay_feedback": [{"body": "Please fix the rubocop violations"}],
        "pr_url": "https://github.com/org/repo/pull/99",
        "pr_node_id": "PR_kwDO123456",
        "pr_diff": "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n",
        "architecture_plan_path": "docs/coordinare-architecture.md",
        "clarifications": [{"questions": ["What framework?"], "answer": "Rails 7"}],
        # Backend selection (037)
        "backend": "claude_code",
        "model": "claude-sonnet-4-20250514",
        # GitHub Enterprise (036)
        "github_api_url": "https://github.example.com/api/v3",
    }


class TestDispatchPayloadContract:
    """Verify that AgentService.dispatch_card() passes through ALL card_context fields."""

    @pytest.mark.asyncio
    async def test_all_card_context_fields_survive_agent_service(self) -> None:
        """Every field in card_context must appear in the wire payload."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _full_card_context()

        await service.dispatch_card(card_context)

        payload = transport.captured_payload
        for key, value in card_context.items():
            assert key in payload, f"Field {key!r} was dropped by AgentService.dispatch_card()"
            assert payload[key] == value, f"Field {key!r} was modified: {payload[key]!r} != {value!r}"

    @pytest.mark.asyncio
    async def test_workspace_info_overlays_card_context(self) -> None:
        """WorkspaceInfo fields are added on top of card_context."""
        from coordinare.workspace import WorkspaceInfo

        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _full_card_context()
        workspace = WorkspaceInfo(
            path=None,
            branch="coordinare/test-branch",
            repo_url="https://github.com/org/repo.git",
            github_token="ghs_test_token",
        )

        await service.dispatch_card(card_context, workspace_info=workspace)

        payload = transport.captured_payload
        # Workspace fields overlay card_context
        assert payload["repo_url"] == "https://github.com/org/repo.git"
        assert payload["branch"] == "coordinare/test-branch"
        assert payload["github_token"] == "ghs_test_token"
        # Original card_context fields still present
        assert payload["role"] == "reviewing"
        assert payload["relay_feedback"] == [{"body": "Please fix the rubocop violations"}]
        assert payload["pr_url"] == "https://github.com/org/repo/pull/99"

    @pytest.mark.asyncio
    async def test_role_field_is_not_dropped(self) -> None:
        """The 'role' field is critical — it determines which performer path runs."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        for role in ["implementing", "reviewing", "security", "qa", "documenting", "architecting", "assessing"]:
            await service.dispatch_card({"role": role, "title": "test", "id": "X"})
            assert transport.captured_payload["role"] == role, f"role={role!r} was dropped"

    @pytest.mark.asyncio
    async def test_relay_feedback_is_not_dropped(self) -> None:
        """relay_feedback carries human review comments — must survive the boundary."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        feedback = [
            {"body": "Fix the rubocop violations"},
            {"body": "Add tests for the new helper"},
        ]

        await service.dispatch_card({"relay_feedback": feedback, "title": "test", "id": "X"})

        assert transport.captured_payload["relay_feedback"] == feedback

    @pytest.mark.asyncio
    async def test_pr_url_and_node_id_are_not_dropped(self) -> None:
        """pr_url and pr_node_id are needed by reviewer/security/QA roles."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_node_id": "PR_kwDO123",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["pr_url"] == "https://github.com/org/repo/pull/99"
        assert transport.captured_payload["pr_node_id"] == "PR_kwDO123"

    @pytest.mark.asyncio
    async def test_pr_diff_is_not_dropped(self) -> None:
        """pr_diff (injected for review roles) must reach the performer wire."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        diff = "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n"
        await service.dispatch_card({
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_diff": diff,
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["pr_diff"] == diff

    @pytest.mark.asyncio
    async def test_backend_and_model_are_not_dropped(self) -> None:
        """Per-role backend/model selection (037) must survive."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "backend": "claude_code",
            "model": "claude-sonnet-4-20250514",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["backend"] == "claude_code"
        assert transport.captured_payload["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_github_api_url_is_not_dropped(self) -> None:
        """GitHub Enterprise URL (036) must survive."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "github_api_url": "https://github.example.com/api/v3",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["github_api_url"] == "https://github.example.com/api/v3"


_has_performer = True
try:
    import performer.models  # noqa: F401
except ModuleNotFoundError:
    _has_performer = False


@pytest.mark.skipif(not _has_performer, reason="performer package not on PYTHONPATH")
class TestScoreModelContract:
    """Verify that the performer's Score model accepts all dispatch payload fields."""

    def test_score_accepts_all_dispatch_fields(self) -> None:
        """Score must not drop any field from a full dispatch payload."""
        from performer.models import Score

        payload = _full_card_context()
        # Add workspace fields (overlaid by AgentService)
        payload["repo_url"] = "https://github.com/org/repo.git"
        payload["branch"] = "coordinare/test-branch"
        payload["github_token"] = "ghs_test"

        score = Score(**payload)

        assert score.title == "Implement breadcrumbs"
        assert score.role == "reviewing"
        assert score.persona_instructions == "Focus on test coverage and code quality."
        assert score.relay_feedback == [{"body": "Please fix the rubocop violations"}]
        assert score.pr_url == "https://github.com/org/repo/pull/99"
        assert score.pr_node_id == "PR_kwDO123456"
        assert score.pr_diff == (
            "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n"
        )
        assert score.backend == "claude_code"
        assert score.model == "claude-sonnet-4-20250514"
        assert score.github_api_url == "https://github.example.com/api/v3"
        assert score.architecture_plan_path == "docs/coordinare-architecture.md"

    def test_score_defaults_for_minimal_payload(self) -> None:
        """Score with only required fields should have safe defaults."""
        from performer.models import Score

        score = Score(
            title="Test",
            repo_url="https://github.com/org/repo",
            branch="main",
        )

        assert score.role == "implementing"
        assert score.persona_instructions == ""
        assert score.relay_feedback == []
        assert score.pr_url == ""
        assert score.pr_node_id == ""
        assert score.pr_diff == ""
        assert score.backend == ""
        assert score.model == ""
        assert score.github_api_url == ""
