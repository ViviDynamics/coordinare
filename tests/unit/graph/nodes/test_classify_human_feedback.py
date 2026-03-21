"""Unit tests for classify_human_feedback (019, T023)."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.classify_human_feedback import (
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
