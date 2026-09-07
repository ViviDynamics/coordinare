"""Model bounds tests for reviewer workflow (spec 169 T004).

Every field is bounded and required. Extra fields are forbidden.
Tests must pass per T004 and fail under mutations per Constitution II.
"""
from __future__ import annotations

import pytest
from performer.workflows.reviewer.models import (
    DEFAULT_CATEGORIES,
    ChangedFile,
    Disposition,
    Finding,
    Hunk,
    ReviewRecord,
)
from pydantic import ValidationError


class TestHunk:
    """Hunk model: header, start_line, end_line, lines."""

    def test_valid_hunk(self):
        """A valid hunk passes."""
        h = Hunk(header="@@ -10,5 +10,7 @@", start_line=10, end_line=16, lines=[" x", "+y"])
        assert h.header == "@@ -10,5 +10,7 @@"
        assert h.start_line == 10
        assert h.end_line == 16
        assert h.lines == [" x", "+y"]

    def test_start_line_minimum_1(self):
        """start_line must be at least 1."""
        with pytest.raises(ValidationError):
            Hunk(header="@@ @@", start_line=0, end_line=1)

    def test_end_line_minimum_1(self):
        """end_line must be at least 1."""
        with pytest.raises(ValidationError):
            Hunk(header="@@ @@", start_line=1, end_line=0)

    def test_extra_forbid(self):
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            Hunk(header="@@ @@", start_line=1, end_line=1, extra_field="bad")


class TestChangedFile:
    """ChangedFile model: path, hunks, fully_in_diff, opened_by_survey."""

    def test_valid_changed_file(self):
        """A valid changed file passes."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[Hunk(header="@@ @@", start_line=1, end_line=1)],
            fully_in_diff=True,
            opened_by_survey=False,
        )
        assert cf.path == "src/foo.py"
        assert cf.fully_in_diff is True
        assert cf.opened_by_survey is False

    def test_path_required(self):
        """path is required."""
        with pytest.raises(ValidationError):
            ChangedFile(hunks=[], fully_in_diff=True)

    def test_hunks_required(self):
        """hunks is required."""
        with pytest.raises(ValidationError):
            ChangedFile(path="src/foo.py", fully_in_diff=True)

    def test_fully_in_diff_required(self):
        """fully_in_diff is required."""
        with pytest.raises(ValidationError):
            ChangedFile(path="src/foo.py", hunks=[])

    def test_opened_by_survey_defaults_false(self):
        """opened_by_survey defaults to False."""
        cf = ChangedFile(path="src/foo.py", hunks=[], fully_in_diff=True)
        assert cf.opened_by_survey is False

    def test_extra_forbid(self):
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            ChangedFile(
                path="src/foo.py",
                hunks=[],
                fully_in_diff=True,
                unknown_field="bad",
            )


class TestFinding:
    """Finding model: path, line, category, problem, why_blocking, evidence, origin."""

    def test_valid_finding_model_origin(self):
        """A valid finding with model origin passes."""
        f = Finding(
            path="src/foo.py",
            line=42,
            category="logic_error",
            problem="Off-by-one error",
            why_blocking="The loop condition is wrong",
            evidence="for i in range(len(x)):",
            origin="model",
        )
        assert f.path == "src/foo.py"
        assert f.line == 42
        assert f.category == "logic_error"
        assert f.origin == "model"

    def test_valid_finding_rule_origin(self):
        """A valid finding with rule origin and empty evidence passes."""
        f = Finding(
            path="src/foo.py",
            line=1,
            category="unaddressed_feedback",
            problem="Prior comment not addressed",
            why_blocking="Must handle all feedback",
            evidence="",
            origin="rule",
        )
        assert f.origin == "rule"
        assert f.evidence == ""

    def test_line_minimum_0(self):
        """line must be >= 0."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=-1,
                category="logic_error",
                problem="x",
                why_blocking="y",
                evidence="z",
                origin="model",
            )

    def test_problem_max_500(self):
        """problem max 500 chars."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x" * 501,
                why_blocking="y",
                evidence="z",
                origin="model",
            )

    def test_problem_min_1(self):
        """problem min 1 char."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="",
                why_blocking="y",
                evidence="z",
                origin="model",
            )

    def test_why_blocking_max_500(self):
        """why_blocking max 500 chars."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="y" * 501,
                evidence="z",
                origin="model",
            )

    def test_why_blocking_min_1(self):
        """why_blocking min 1 char."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="",
                evidence="z",
                origin="model",
            )

    def test_evidence_max_200(self):
        """evidence max 200 chars."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="y",
                evidence="z" * 201,
                origin="model",
            )

    def test_evidence_empty_only_for_rule(self):
        """evidence must be non-empty for model-origin findings."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="y",
                evidence="",
                origin="model",
            )

    def test_category_in_set(self):
        """FR-018: the category set is a workflow parameter. The model-facing schema
        pins ``category`` to the configured set (an unknown one is a schema
        violation, reprompted once); the record model keeps a bounded string so
        the security role can carry its own set through the same record."""
        from performer.workflows.reviewer.models import DEFAULT_CATEGORIES, model_findings_schema

        Finding(path="src/foo.py", line=1, category="logic_error", problem="x", why_blocking="y", evidence="z", origin="model")
        schema = model_findings_schema(DEFAULT_CATEGORIES)
        good = {"path": "src/foo.py", "line": 1, "category": "logic_error", "problem": "x", "why_blocking": "y", "evidence": "z"}
        schema.model_validate({"findings": [good], "dispositions": []})
        with pytest.raises(ValidationError):
            schema.model_validate({"findings": [{**good, "category": "invalid_category"}], "dispositions": []})
        # a verdict key is a schema violation: the verdict is derived by code (FR-005)
        with pytest.raises(ValidationError):
            schema.model_validate({"findings": [good], "dispositions": [], "verdict": "approved"})
        # another role's set is accepted by the same builder
        own = model_findings_schema(("injection", "secrets"))
        own.model_validate({"findings": [{**good, "category": "secrets"}], "dispositions": []})
        with pytest.raises(ValidationError):
            own.model_validate({"findings": [good], "dispositions": []})

    def test_origin_literal(self):
        """origin must be model or rule."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="y",
                evidence="z",
                origin="invalid",  # type: ignore
            )

    def test_extra_forbid(self):
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            Finding(
                path="src/foo.py",
                line=1,
                category="logic_error",
                problem="x",
                why_blocking="y",
                evidence="z",
                origin="model",
                extra_field="bad",
            )


class TestDisposition:
    """Disposition model: prior_comment_id, status, finding_index."""

    def test_valid_disposition_fixed(self):
        """A valid disposition with fixed status passes."""
        d = Disposition(prior_comment_id="comment-123", status="fixed")
        assert d.prior_comment_id == "comment-123"
        assert d.status == "fixed"
        assert d.finding_index is None

    def test_valid_disposition_not_fixed_with_index(self):
        """A valid disposition with not_fixed status and finding_index passes."""
        d = Disposition(
            prior_comment_id="comment-123",
            status="not_fixed",
            finding_index=0,
        )
        assert d.status == "not_fixed"
        assert d.finding_index == 0

    def test_prior_comment_id_required(self):
        """prior_comment_id is required."""
        with pytest.raises(ValidationError):
            Disposition(status="fixed")

    def test_status_required(self):
        """status is required."""
        with pytest.raises(ValidationError):
            Disposition(prior_comment_id="comment-123")

    def test_status_literal(self):
        """status must be fixed or not_fixed."""
        with pytest.raises(ValidationError):
            Disposition(
                prior_comment_id="comment-123",
                status="invalid",  # type: ignore
            )

    def test_extra_forbid(self):
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            Disposition(
                prior_comment_id="comment-123",
                status="fixed",
                unknown_field="bad",
            )


class TestReviewRecord:
    """ReviewRecord model: comprehensive review workflow record."""

    def test_valid_review_record_minimal(self):
        """A minimal valid review record passes."""
        rr = ReviewRecord(
            changed_files=[],
            diff_truncated=False,
            verdict="approved",
            covered_files=[],
        )
        assert rr.changed_files == []
        assert rr.diff_truncated is False
        assert rr.verdict == "approved"
        assert rr.covered_files == []
        assert rr.unread_files == []
        assert rr.survey_commands == []

    def test_verdict_literal(self):
        """verdict must be approved, changes_requested, or env_blocked."""
        ReviewRecord(
            changed_files=[],
            diff_truncated=False,
            verdict="approved",
            covered_files=[],
        )
        ReviewRecord(
            changed_files=[],
            diff_truncated=False,
            verdict="changes_requested",
            covered_files=[],
        )
        ReviewRecord(
            changed_files=[],
            diff_truncated=False,
            verdict="env_blocked",
            covered_files=[],
        )
        with pytest.raises(ValidationError):
            ReviewRecord(
                changed_files=[],
                diff_truncated=False,
                verdict="invalid",  # type: ignore
                covered_files=[],
            )

    def test_changed_files_required(self):
        """changed_files is required."""
        with pytest.raises(ValidationError):
            ReviewRecord(diff_truncated=False, verdict="approved", covered_files=[])

    def test_diff_truncated_required(self):
        """diff_truncated is required."""
        with pytest.raises(ValidationError):
            ReviewRecord(changed_files=[], verdict="approved", covered_files=[])

    def test_verdict_required(self):
        """verdict is required."""
        with pytest.raises(ValidationError):
            ReviewRecord(changed_files=[], diff_truncated=False, covered_files=[])

    def test_covered_files_required(self):
        """covered_files is required."""
        with pytest.raises(ValidationError):
            ReviewRecord(changed_files=[], diff_truncated=False, verdict="approved")

    def test_findings_max_30(self):
        """findings list max 30 items."""
        findings = [
            Finding(
                path="src/foo.py",
                line=i,
                category="logic_error",
                problem="Issue",
                why_blocking="Blocking",
                evidence="evidence",
                origin="model",
            )
            for i in range(1, 32)  # 31 findings
        ]
        with pytest.raises(ValidationError):
            ReviewRecord(
                changed_files=[],
                diff_truncated=False,
                verdict="changes_requested",
                covered_files=[],
                findings_before_gate=findings,
            )

    def test_extra_forbid(self):
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            ReviewRecord(
                changed_files=[],
                diff_truncated=False,
                verdict="approved",
                covered_files=[],
                unknown_field="bad",
            )


class TestDefaultCategories:
    """DEFAULT_CATEGORIES constant is the fixed set."""

    def test_default_categories_complete(self):
        """DEFAULT_CATEGORIES contains all required categories."""
        expected = {
            "logic_error",
            "test_missing",
            "style",
            "performance",
            "security",
            "documentation_by_implementer",
            "unaddressed_feedback",
        }
        assert set(DEFAULT_CATEGORIES) == expected
