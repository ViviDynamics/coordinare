"""Tests for spec 169 review_findings dispatch functions."""

from coordinare.graph.nodes.dispatch_performer import (
    inject_review_findings,
    reset_review_findings_for_reviewer,
)


class TestResetReviewFindingsForReviewer:
    """Tests for reset_review_findings_for_reviewer function."""

    def test_reset_clears_findings_on_reviewer_dispatch(self):
        """Calling on reviewing stage clears review_findings, returns True."""
        state = {
            "review_findings": {
                "verdict": "changes_requested",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
            }
        }

        result = reset_review_findings_for_reviewer(state, "reviewing")

        assert result is True
        assert state["review_findings"] is None

    def test_reset_returns_false_on_non_reviewing_stage(self):
        """Calling on non-reviewing stage does not clear, returns False."""
        state = {
            "review_findings": {
                "verdict": "changes_requested",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
            }
        }

        result = reset_review_findings_for_reviewer(state, "implementing")

        assert result is False
        assert state["review_findings"] is not None

    def test_reset_when_review_findings_already_none(self):
        """Resetting when already None returns False (nothing to clear)."""
        state = {"review_findings": None}

        result = reset_review_findings_for_reviewer(state, "reviewing")

        assert result is False
        assert state["review_findings"] is None

    def test_reset_returns_false_for_other_stages(self):
        """Resetting on any non-reviewing stage returns False."""
        state = {"review_findings": {"verdict": "approved", "changed_files": []}}

        for stage in ["implementing", "architecting", "assessing", "qa", "closing_review"]:
            result = reset_review_findings_for_reviewer(state, stage)
            assert result is False, f"Should return False for stage {stage}"

    def test_reset_handles_missing_review_findings_key(self):
        """Resetting when review_findings key is missing returns False and creates None."""
        state = {}

        result = reset_review_findings_for_reviewer(state, "reviewing")

        assert result is False
        assert state["review_findings"] is None


class TestInjectReviewFindings:
    """Tests for inject_review_findings function."""

    def test_inject_review_findings_into_implementing_stage(self):
        """Inject review_findings only for implementing stage."""
        state = {
            "review_findings": {
                "verdict": "changes_requested",
                "changed_files": [{"path": "src/main.py"}],
                "diff_truncated": False,
                "covered_files": ["src/main.py"],
                "findings_before_gate": [
                    {
                        "path": "src/main.py",
                        "line": 1,
                        "category": "style",
                        "problem": "Missing docstring",
                        "why_blocking": "Documentation required",
                        "evidence": "def foo():",
                        "origin": "model",
                    }
                ],
            }
        }
        card_context = {}

        inject_review_findings(card_context, state, performer_stage="implementing")

        assert "review_findings" in card_context
        assert card_context["review_findings"] is not None
        assert card_context["review_findings"]["verdict"] == "changes_requested"

    def test_do_not_inject_for_non_implementing_stage(self):
        """Do not inject review_findings for non-implementing stages."""
        state = {
            "review_findings": {
                "verdict": "changes_requested",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
            }
        }
        card_context = {}

        inject_review_findings(card_context, state, performer_stage="reviewing")

        assert "review_findings" not in card_context or card_context.get("review_findings") is None

    def test_do_not_inject_when_none(self):
        """Do not inject when review_findings is None."""
        state = {"review_findings": None}
        card_context = {}

        inject_review_findings(card_context, state, performer_stage="implementing")

        assert "review_findings" not in card_context or card_context.get("review_findings") is None

    def test_inject_deep_copies_findings(self):
        """Injected findings are deep copied, not references."""
        original_findings = {
            "verdict": "changes_requested",
            "changed_files": [{"path": "src/main.py"}],
            "diff_truncated": False,
            "covered_files": ["src/main.py"],
        }
        state = {"review_findings": original_findings}
        card_context = {}

        inject_review_findings(card_context, state, performer_stage="implementing")

        # Modify the original
        original_findings["verdict"] = "approved"

        # Injected copy should not be affected
        assert card_context["review_findings"]["verdict"] == "changes_requested"

    def test_inject_only_for_implementing_all_other_stages(self):
        """Verify inject only happens for implementing stage."""
        state = {
            "review_findings": {
                "verdict": "changes_requested",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
            }
        }

        for stage in ["reviewing", "architecting", "assessing", "qa", "closing_review", "documenting", "security"]:
            card_context = {}
            inject_review_findings(card_context, state, performer_stage=stage)
            assert (
                "review_findings" not in card_context
                or card_context.get("review_findings") is None
            ), f"Should not inject for stage {stage}"

    def test_inject_empty_findings_list_not_injected(self):
        """When review_findings has empty findings list but is not None, still inject."""
        state = {
            "review_findings": {
                "verdict": "approved",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
                "findings_before_gate": [],
            }
        }
        card_context = {}

        inject_review_findings(card_context, state, performer_stage="implementing")

        # Should still inject even if findings list is empty
        assert card_context["review_findings"] is not None
        assert card_context["review_findings"]["verdict"] == "approved"


def test_inject_rejects_a_record_without_a_verdict_like_the_lift_does():
    """Review finding: inject validated changed_files only; the lift and the daemon require verdict too."""
    from coordinare.graph.nodes.dispatch_performer import inject_review_findings

    state = {"review_findings": {"changed_files": [], "findings": []}}
    card_context: dict = {}
    inject_review_findings(card_context, state, performer_stage="implementing")
    assert "review_findings" not in card_context
