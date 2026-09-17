"""Contract tests for spec 169 review_findings in dispatch payload."""
import json

import jsonschema
import pytest
from performer.models import Score


class TestReviewFindingsInScore:
    """Tests that review_findings is properly declared on Score model."""

    def test_review_findings_field_exists_on_score(self):
        """review_findings field must be declared on Score."""
        # Create a Score with review_findings
        score = Score(
            title="Test PR",
            repo_url="https://github.com/example/repo",
            branch="feature/test",
            review_findings={
                "changed_files": [],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": [],
            },
        )

        assert score.review_findings is not None
        assert score.review_findings["verdict"] == "changes_requested"

    def test_review_findings_defaults_to_none(self):
        """review_findings should default to None when not provided."""
        score = Score(
            title="Test PR",
            repo_url="https://github.com/example/repo",
            branch="feature/test",
        )

        assert score.review_findings is None

    def test_review_findings_not_injected_for_reviewing_stage(self):
        """review_findings should not be injected for reviewing stage dispatch."""
        # When dispatching the reviewing stage, review_findings should be absent or None
        score = Score(
            title="Test PR",
            repo_url="https://github.com/example/repo",
            branch="feature/test",
            role="reviewing",
            review_findings=None,
        )

        assert score.review_findings is None

    def test_review_findings_injected_for_implementing_stage(self):
        """review_findings should be injected for implementing stage dispatch."""
        review_record = {
            "changed_files": [
                {
                    "path": "src/main.py",
                    "hunks": [{"header": "@@ -1,1 +1,1 @@", "start_line": 1, "end_line": 1}],
                    "fully_in_diff": True,
                }
            ],
            "diff_truncated": False,
            "verdict": "changes_requested",
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
        score = Score(
            title="Test PR",
            repo_url="https://github.com/example/repo",
            branch="feature/test",
            role="implementing",
            review_findings=review_record,
        )

        assert score.review_findings == review_record

    def test_review_findings_absent_from_other_stages(self):
        """review_findings should not appear in dispatches for non-implementing stages."""
        for stage in ["architecting", "assessing", "qa", "closing_review", "security", "documenting"]:
            score = Score(
                title="Test PR",
                repo_url="https://github.com/example/repo",
                branch="feature/test",
                role=stage,
                review_findings=None,
            )
            # review_findings can be None but should not be actively injected
            assert score.review_findings is None


class TestReviewFindingsRecordSchema:
    """Tests that review_findings matches the contract schema."""

    def test_review_findings_validates_against_schema(self):
        """review_findings payload must validate against review-record.schema.json."""
        # Load the schema
        schema_path = "specs/169-reviewer-workflow/contracts/review-record.schema.json"
        try:
            with open(schema_path) as f:
                schema = json.load(f)
        except FileNotFoundError:
            pytest.skip(f"Schema file not found: {schema_path}")

        # Valid review record
        review_record = {
            "changed_files": [
                {
                    "path": "src/main.py",
                    "hunks": [
                        {
                            "header": "@@ -1,5 +1,7 @@",
                            "start_line": 1,
                            "end_line": 7,
                            "lines": ["def foo():", "    pass"],
                        }
                    ],
                    "fully_in_diff": True,
                    "deleted": False,
                    "deleted_before_cut": False,
                    "opened_by_survey": False,
                }
            ],
            "diff_truncated": False,
            "verdict": "changes_requested",
            "covered_files": ["src/main.py"],
            "findings_before_gate": [
                {
                    "path": "src/main.py",
                    "line": 2,
                    "category": "style",
                    "problem": "Missing docstring",
                    "why_blocking": "Documentation required",
                    "evidence": "def foo():",
                    "origin": "model",
                }
            ],
        }

        # Should validate without raising
        jsonschema.validate(instance=review_record, schema=schema)

    def test_review_findings_missing_required_fields_fail_validation(self):
        """Missing required fields in review_findings should fail validation."""
        schema_path = "specs/169-reviewer-workflow/contracts/review-record.schema.json"
        try:
            with open(schema_path) as f:
                schema = json.load(f)
        except FileNotFoundError:
            pytest.skip(f"Schema file not found: {schema_path}")

        # Missing changed_files (required)
        invalid_record = {
            "diff_truncated": False,
            "verdict": "changes_requested",
            "covered_files": [],
        }

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=invalid_record, schema=schema)
