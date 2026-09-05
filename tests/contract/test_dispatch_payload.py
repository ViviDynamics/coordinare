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
        # 164: role workflow fields -- the "all fields survive" test must cover
        # them too, not only their dedicated tests (round-one finding that never
        # received a verdict; dispositioned by hand).
        "workflow": "qa",
        "workflow_env": {"PORT": "3000", "QA_APP_START_COMMAND": "python app.py"},
        "qa_findings": [{"file": "a.py", "line": 1, "category": "unmet_criterion", "severity": "high"}],
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
    async def test_workflow_is_not_dropped(self) -> None:
        """164: the workflow name selects a multi-step role workflow inside the
        performer.  Score uses extra="ignore", so an unregistered field is
        dropped in transit — the role would look configured and never run its
        workflow, with no error anywhere.  Registered in the payload contract."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({"workflow": "qa", "title": "test", "id": "X"})

        assert transport.captured_payload["workflow"] == "qa"

    @pytest.mark.asyncio
    async def test_workflow_env_is_not_dropped(self) -> None:
        """164: the operator's app boot settings for a role workflow. Before
        this, QA_APP_START_COMMAND was an env var nothing in production set."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        env = {"QA_APP_START_COMMAND": "bin/rails s -p 3000", "PORT": "3000"}

        await service.dispatch_card({"workflow_env": env, "title": "test", "id": "X"})

        assert transport.captured_payload["workflow_env"] == env

    @pytest.mark.asyncio
    async def test_qa_findings_are_not_dropped(self) -> None:
        """164: the repair brief from the previous QA round, carried into the
        implementer's dispatch the way scanner_findings already is."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        findings = [
            {
                "file": "app/models/user.rb",
                "line": 42,
                "category": "unexpected_regression",
                "severity": "high",
                "criterion": "Users can sign in",
                "expected": "password field present",
                "observed": "password field absent",
            }
        ]

        await service.dispatch_card(
            {"qa_findings": findings, "title": "test", "id": "X"}
        )

        assert transport.captured_payload["qa_findings"] == findings

    @pytest.mark.asyncio
    async def test_disputed_feedback_is_not_dropped(self) -> None:
        """126: disputed_feedback carries dispute-adjudication context for the
        raising stage — must survive the boundary."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        disputes = [{"id": "fb-1", "body": "wrong finding", "reason": "already correct"}]

        await service.dispatch_card(
            {"disputed_feedback": disputes, "title": "test", "id": "X"}
        )

        assert transport.captured_payload["disputed_feedback"] == disputes

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
