"""Unit tests for classify_human_feedback (019, T023)."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.classify_human_feedback import (
    _classify_with_ai,
    classify_feedback_concerns,
    classify_human_feedback,
)
from coordinare.graph.state import initial_state

# ---------------------------------------------------------------------------
# classify_feedback_concerns (keyword classification)
# ---------------------------------------------------------------------------


class TestClassifyFeedbackConcerns:
    def test_implementation_concern(self) -> None:
        reviews = [{"body": "The authentication logic is broken — it doesn't handle expired tokens."}]
        concerns = classify_feedback_concerns(reviews)
        assert "implementation" in concerns

    def test_architecture_concern(self) -> None:
        reviews = [{"body": "This module has too much coupling with the database layer. Consider a cleaner abstraction."}]
        concerns = classify_feedback_concerns(reviews)
        assert "architecture" in concerns

    def test_security_concern(self) -> None:
        reviews = [{"body": "There's a potential XSS vulnerability in the user input handling."}]
        concerns = classify_feedback_concerns(reviews)
        assert "security" in concerns

    def test_documentation_concern(self) -> None:
        reviews = [{"body": "The README is outdated and doesn't mention the new API endpoint."}]
        concerns = classify_feedback_concerns(reviews)
        assert "documentation" in concerns

    def test_qa_concern(self) -> None:
        reviews = [{"body": "Test coverage for the new module is insufficient — add regression tests."}]
        concerns = classify_feedback_concerns(reviews)
        assert "qa" in concerns

    def test_review_concern(self) -> None:
        reviews = [{"body": "The naming convention for these variables is inconsistent."}]
        concerns = classify_feedback_concerns(reviews)
        assert "review" in concerns

    def test_multiple_concerns(self) -> None:
        reviews = [
            {"body": "The authentication logic is broken (bug) and there's also a security vulnerability."},
        ]
        concerns = classify_feedback_concerns(reviews)
        assert "implementation" in concerns
        assert "security" in concerns

    def test_no_concerns_found(self) -> None:
        reviews = [{"body": "Looks good to me!"}]
        concerns = classify_feedback_concerns(reviews)
        assert concerns == []

    def test_empty_reviews(self) -> None:
        assert classify_feedback_concerns([]) == []

    def test_comments_field_scanned(self) -> None:
        reviews = [{"body": "", "comments": [{"body": "This function has a bug"}]}]
        concerns = classify_feedback_concerns(reviews)
        assert "implementation" in concerns


# ---------------------------------------------------------------------------
# classify_human_feedback node
# ---------------------------------------------------------------------------


class TestClassifyHumanFeedback:
    @pytest.mark.asyncio
    async def test_implementation_concern_routes_to_implementing(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["performer_stage"] = "reviewing"
        state["pending_reviews"] = [{"body": "The code has a bug in the error handling logic."}]

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "implementing"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_architecture_concern_routes_to_architecting(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["architecting", "implementing", "reviewing"]
        state["performer_stage"] = "reviewing"
        state["pending_reviews"] = [{"body": "The module structure needs a design rethink."}]

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "architecting"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_security_concern_routes_to_security(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "security", "qa"]
        state["performer_stage"] = "qa"
        state["pending_reviews"] = [{"body": "There's a potential injection vulnerability here."}]

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "security"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_documentation_concern_routes_to_documenting(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "documenting"]
        state["performer_stage"] = "documenting"
        state["pending_reviews"] = [{"body": "The documentation needs updating."}]

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "documenting"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_unknown_concern_defaults_to_implementing(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["performer_stage"] = "reviewing"
        state["pending_reviews"] = [{"body": "Looks good to me!"}]

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "implementing"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_concern_for_unconfigured_role_falls_back(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "qa"]  # no security stage
        state["performer_stage"] = "qa"
        state["pending_reviews"] = [{"body": "There's a security vulnerability with token handling."}]

        result = await classify_human_feedback(state)

        # Security stage not in lifecycle → falls back to implementing
        assert result["performer_stage"] == "implementing"
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_pr_comments_included_as_relay_feedback(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing"]
        state["performer_stage"] = "implementing"
        reviews = [{"body": "Fix this bug please."}]
        state["pending_reviews"] = reviews

        result = await classify_human_feedback(state)

        assert result.get("relay_feedback") == reviews
        assert result["pending_reviews"] == []

    @pytest.mark.asyncio
    async def test_no_reviews_stays_in_monitoring_pr(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing"]
        state["performer_stage"] = "implementing"
        state["pending_reviews"] = []

        result = await classify_human_feedback(state)

        assert result["phase"] == "monitoring_pr"

    @pytest.mark.asyncio
    async def test_dispatch_state_is_reset(self) -> None:
        state = initial_state()
        state["lifecycle_sequence"] = ["implementing"]
        state["performer_stage"] = "implementing"
        state["pending_reviews"] = [{"body": "There's a logic error."}]
        state["agent_dispatch"] = {"session_id": "old-session"}

        result = await classify_human_feedback(state)

        assert result["agent_dispatch"] == {}
        assert result["agent_dispatch_at"] is None
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_earliest_concern_wins(self) -> None:
        """When multiple concerns are found, route to the earliest in lifecycle."""
        state = initial_state()
        state["lifecycle_sequence"] = ["architecting", "implementing", "security", "documenting"]
        state["performer_stage"] = "documenting"
        # Comment mentions both architecture and documentation
        state["pending_reviews"] = [
            {"body": "The module design pattern is wrong and the documentation is outdated."},
        ]

        result = await classify_human_feedback(state)

        # "architecting" comes before "documenting" in lifecycle
        assert result["performer_stage"] == "architecting"


# ---------------------------------------------------------------------------
# 029 — AI Classification tests
# ---------------------------------------------------------------------------


class TestAIClassification:
    """Tests for _classify_with_ai and AI-first routing."""

    @pytest.mark.asyncio
    async def test_ai_returns_valid_classification(self) -> None:
        """AI backend returns valid classification → used."""
        import json
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess = AsyncMock(return_value={
            "rationale": json.dumps([
                {"concern": "security", "confidence": 0.9},
                {"concern": "implementation", "confidence": 0.8},
            ])
        })

        reviews = [{"body": "There's a vulnerability in the auth flow"}]
        result = await _classify_with_ai(reviews, backend)

        assert result is not None
        assert "security" in result
        assert "implementation" in result

    @pytest.mark.asyncio
    async def test_ai_low_confidence_returns_none(self) -> None:
        """AI returns all concerns below threshold → returns None (triggers keyword fallback)."""
        import json
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess = AsyncMock(return_value={
            "rationale": json.dumps([
                {"concern": "implementation", "confidence": 0.3},
                {"concern": "review", "confidence": 0.2},
            ])
        })

        reviews = [{"body": "minor style issue"}]
        result = await _classify_with_ai(reviews, backend)

        assert result is None  # all below 0.6 threshold

    @pytest.mark.asyncio
    async def test_ai_backend_error_returns_none(self) -> None:
        """AI backend raises → returns None (triggers keyword fallback)."""
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess.side_effect = ConnectionError("backend down")

        reviews = [{"body": "there's a bug"}]
        result = await _classify_with_ai(reviews, backend)

        assert result is None

    @pytest.mark.asyncio
    async def test_ai_malformed_response_returns_none(self) -> None:
        """AI returns non-JSON → returns None."""
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess.return_value = {"rationale": "not json at all"}

        reviews = [{"body": "fix this"}]
        result = await _classify_with_ai(reviews, backend)

        assert result is None

    @pytest.mark.asyncio
    async def test_classify_uses_ai_when_available(self) -> None:
        """classify_human_feedback uses AI classification when backend is available."""
        import json
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess = AsyncMock(return_value={
            "rationale": json.dumps([{"concern": "security", "confidence": 0.95}])
        })

        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "security"]
        state["performer_stage"] = "implementing"
        state["pending_reviews"] = [{"body": "I think there might be a vulnerability"}]
        state["assessment_backend"] = backend
        state["github_service"] = AsyncMock(move_card=AsyncMock())
        state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}

        result = await classify_human_feedback(state)

        assert result["performer_stage"] == "security"
        backend.assess.assert_called_once()

    @pytest.mark.asyncio
    async def test_classify_falls_back_to_keywords_on_ai_failure(self) -> None:
        """When AI fails, keyword classification is used."""
        from unittest.mock import AsyncMock

        backend = AsyncMock()
        backend.assess.side_effect = ConnectionError("down")

        state = initial_state()
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["performer_stage"] = "reviewing"
        state["pending_reviews"] = [{"body": "The code has a bug in the error handling logic."}]
        state["assessment_backend"] = backend
        state["github_service"] = AsyncMock(move_card=AsyncMock())
        state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}

        result = await classify_human_feedback(state)

        # Keyword matching should pick up "bug" → implementation
        assert result["performer_stage"] == "implementing"


# ---------------------------------------------------------------------------
# 031 — /coordinare command parsing tests
# ---------------------------------------------------------------------------


class TestParseCommandsParsing:
    """Test _parse_coordinare_commands function."""

    def test_skip_command(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [{"body": "LGTM but /coordinare skip-reviewer please"}]
        result = _parse_coordinare_commands(reviews)
        assert result == {"action": "skip"}

    def test_restart_from_command(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [{"body": "/coordinare restart-from architect"}]
        lifecycle = ["implementing", "architecting", "reviewing"]
        result = _parse_coordinare_commands(reviews, lifecycle)
        assert result == {"action": "restart", "target_stage": "architecting"}

    def test_veto_command(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [{"body": "/coordinare veto"}]
        result = _parse_coordinare_commands(reviews)
        assert result == {"action": "veto"}

    def test_no_command(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [{"body": "Looks good, minor style nit."}]
        result = _parse_coordinare_commands(reviews)
        assert result is None

    def test_mixed_text_with_command(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [
            {"body": "I think we need to go back. /coordinare restart-from implementing"},
        ]
        lifecycle = ["implementing", "reviewing"]
        result = _parse_coordinare_commands(reviews, lifecycle)
        assert result == {"action": "restart", "target_stage": "implementing"}

    def test_case_insensitive(self) -> None:
        from coordinare.graph.nodes.classify_human_feedback import _parse_coordinare_commands

        reviews = [{"body": "/Coordinare VETO"}]
        result = _parse_coordinare_commands(reviews)
        assert result == {"action": "veto"}


class TestClassifyHumanFeedbackCommands:
    """Integration tests for /coordinare commands in classify_human_feedback."""

    @pytest.mark.asyncio
    async def test_skip_command_sets_override(self) -> None:
        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare skip-reviewer"}]
        state["lifecycle_sequence"] = ["implementing", "reviewing"]

        result = await classify_human_feedback(state)

        assert result["pending_override"] == {"action": "skip"}
        assert result["phase"] == "dispatching"
        assert result["pending_reviews"] == []

    @pytest.mark.asyncio
    async def test_veto_command_sets_override(self) -> None:
        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare veto"}]
        state["lifecycle_sequence"] = ["implementing"]

        result = await classify_human_feedback(state)

        assert result["pending_override"] == {"action": "veto"}
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_command_skipped_when_override_pending(self) -> None:
        """If dashboard already queued an override, PR command is ignored (FR-010)."""
        from unittest.mock import AsyncMock

        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare skip-reviewer"}]
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["pending_override"] = {"action": "veto"}  # dashboard override
        state["github_service"] = AsyncMock(move_card=AsyncMock())
        state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}

        result = await classify_human_feedback(state)

        # Dashboard veto preserved, PR skip command ignored
        assert result["pending_override"] == {"action": "veto"}
        assert result["phase"] == "dispatching"  # normal classification path

    @pytest.mark.asyncio
    async def test_restart_from_valid_stage_queues_override(self) -> None:
        """Valid restart-from with stage name queues override."""
        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare restart-from implementing"}]
        state["lifecycle_sequence"] = ["implementing", "reviewing"]

        result = await classify_human_feedback(state)

        assert result["pending_override"] == {"action": "restart", "target_stage": "implementing"}
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_restart_from_role_noun_resolved_to_stage(self) -> None:
        """restart-from with role noun (architect) resolves to stage (architecting)."""
        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare restart-from architect"}]
        state["lifecycle_sequence"] = ["implementing", "architecting", "reviewing"]

        result = await classify_human_feedback(state)

        assert result["pending_override"] == {"action": "restart", "target_stage": "architecting"}
        assert result["phase"] == "dispatching"

    @pytest.mark.asyncio
    async def test_restart_from_invalid_role_falls_through(self) -> None:
        """Invalid restart-from target falls through to normal classification."""
        from unittest.mock import AsyncMock

        state = initial_state()
        state["pending_reviews"] = [{"body": "/coordinare restart-from nonexistent"}]
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["github_service"] = AsyncMock(move_card=AsyncMock())
        state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}

        result = await classify_human_feedback(state)

        # Invalid role → falls through to normal classification
        assert result.get("pending_override") is None
        assert result["phase"] == "dispatching"  # normal classification path

    @pytest.mark.asyncio
    async def test_normal_text_no_override(self) -> None:
        """Regular PR comment without /coordinare prefix → normal classification."""
        from unittest.mock import AsyncMock

        state = initial_state()
        state["pending_reviews"] = [{"body": "There's a bug in the login flow"}]
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["github_service"] = AsyncMock(move_card=AsyncMock())
        state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}

        result = await classify_human_feedback(state)

        assert result.get("pending_override") is None
        assert result["phase"] == "dispatching"  # normal feedback path
