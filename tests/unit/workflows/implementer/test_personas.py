"""Tests for implementer persona templates (spec 167 T017).

Tests verify:
1. Every template renders with its placeholders from data-model.md
2. Missing placeholder raises KeyError
3. Each template contains forbidden lines verbatim (per data-model.md table)
"""
import pytest
from performer.workflows.implementer import personas


class TestPersonasRender:
    """Test persona template rendering with placeholders."""

    def test_tests_persona_renders_all_placeholders(self):
        """TESTS persona renders with all required placeholders."""
        result = personas.TESTS.format(
            milestone_goal="User can sign in",
            scope_paths="auth/",
            done_when="signin page appears",
            test_conventions="pytest with test_*.py",
        )
        assert "User can sign in" in result
        assert "auth/" in result
        assert "signin page appears" in result
        assert "pytest with test_*.py" in result

    def test_tests_persona_forbids_missing_placeholder(self):
        """TESTS persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.TESTS.format(
                milestone_goal="User can sign in",
                scope_paths="auth/",
                done_when="signin page appears",
                # Missing: test_conventions
            )

    def test_tests_persona_contains_forbidden_lines(self):
        """TESTS persona contains all forbidden lines verbatim."""
        rendered = personas.TESTS.format(
            milestone_goal="test",
            scope_paths="auth/",
            done_when="ok",
            test_conventions="pytest",
        )
        assert "Do not change source files." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered

    def test_implement_persona_renders_all_placeholders(self):
        """IMPLEMENT persona renders with all required placeholders."""
        result = personas.IMPLEMENT.format(
            milestone_goal="User can sign in",
            scope_paths="auth/",
            done_when="signin page appears",
            failing_tests=["test_signin_email", "test_signin_password"],
            failure_excerpt="AssertionError: expected True",
        )
        assert "User can sign in" in result
        assert "auth/" in result
        assert "signin page appears" in result
        assert "test_signin_email" in result
        assert "AssertionError: expected True" in result

    def test_implement_persona_forbids_missing_placeholder(self):
        """IMPLEMENT persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.IMPLEMENT.format(
                milestone_goal="User can sign in",
                scope_paths="auth/",
                done_when="signin page appears",
                failing_tests=["test_signin"],
                # Missing: failure_excerpt
            )

    def test_implement_persona_contains_forbidden_lines(self):
        """IMPLEMENT persona contains all forbidden lines verbatim."""
        rendered = personas.IMPLEMENT.format(
            milestone_goal="test",
            scope_paths="auth/",
            done_when="ok",
            failing_tests=["test_1"],
            failure_excerpt="output",
        )
        assert "Make exactly these tests pass." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered

    def test_repair_tests_persona_renders_all_placeholders(self):
        """REPAIR_TESTS persona renders with all required placeholders."""
        result = personas.REPAIR_TESTS.format(
            milestone_goal="User can sign in",
            passing_test_files=["tests/auth/test_signin.py"],
        )
        assert "User can sign in" in result
        assert "tests/auth/test_signin.py" in result

    def test_repair_tests_persona_forbids_missing_placeholder(self):
        """REPAIR_TESTS persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.REPAIR_TESTS.format(
                milestone_goal="User can sign in",
                # Missing: passing_test_files
            )

    def test_repair_tests_persona_contains_forbidden_lines(self):
        """REPAIR_TESTS persona contains all forbidden lines."""
        rendered = personas.REPAIR_TESTS.format(
            milestone_goal="test",
            passing_test_files=["test.py"],
        )
        # REPAIR_TESTS has its own line plus TESTS lines
        assert "Do not change source files." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered

    def test_repair_implement_persona_renders_all_placeholders(self):
        """REPAIR_IMPLEMENT persona renders with all required placeholders."""
        result = personas.REPAIR_IMPLEMENT.format(
            milestone_goal="User can sign in",
            scope_paths="auth/",
            done_when="signin page appears",
            failing_tests=["test_signin_email"],
            failure_excerpt="AssertionError: expected True",
        )
        assert "User can sign in" in result
        assert "auth/" in result
        assert "signin page appears" in result
        assert "test_signin_email" in result
        assert "AssertionError: expected True" in result

    def test_repair_implement_persona_forbids_missing_placeholder(self):
        """REPAIR_IMPLEMENT persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.REPAIR_IMPLEMENT.format(
                milestone_goal="User can sign in",
                scope_paths="auth/",
                done_when="signin page appears",
                failing_tests=["test_signin"],
                # Missing: failure_excerpt
            )

    def test_repair_implement_persona_contains_forbidden_lines(self):
        """REPAIR_IMPLEMENT persona contains all forbidden lines."""
        rendered = personas.REPAIR_IMPLEMENT.format(
            milestone_goal="test",
            scope_paths="auth/",
            done_when="ok",
            failing_tests=["test_1"],
            failure_excerpt="output",
        )
        assert "Make exactly these tests pass." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered

    def test_repair_quality_persona_renders_all_placeholders(self):
        """REPAIR_QUALITY persona renders with all required placeholders."""
        result = personas.REPAIR_QUALITY.format(
            command="ruff check .",
            tool_output="error: undefined variable",
        )
        assert "ruff check ." in result
        assert "error: undefined variable" in result

    def test_repair_quality_persona_forbids_missing_placeholder(self):
        """REPAIR_QUALITY persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.REPAIR_QUALITY.format(
                command="ruff check .",
                # Missing: tool_output
            )

    def test_repair_quality_persona_contains_forbidden_lines(self):
        """REPAIR_QUALITY persona contains all forbidden lines."""
        rendered = personas.REPAIR_QUALITY.format(
            command="ruff check .",
            tool_output="output",
        )
        assert "Fix only what this tool reports." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered

    def test_repair_ci_persona_renders_all_placeholders(self):
        """REPAIR_CI persona renders with all required placeholders."""
        result = personas.REPAIR_CI.format(
            check_name="Tests",
            log_excerpt="AssertionError: test failed",
        )
        assert "Tests" in result
        assert "AssertionError: test failed" in result

    def test_repair_ci_persona_forbids_missing_placeholder(self):
        """REPAIR_CI persona raises KeyError if placeholder missing."""
        with pytest.raises(KeyError):
            personas.REPAIR_CI.format(
                check_name="Tests",
                # Missing: log_excerpt
            )

    def test_repair_ci_persona_contains_forbidden_lines(self):
        """REPAIR_CI persona contains all forbidden lines."""
        rendered = personas.REPAIR_CI.format(
            check_name="Tests",
            log_excerpt="output",
        )
        assert "Fix what this check reports." in rendered
        assert "Do not create or edit documentation." in rendered
        assert "Do not commit." in rendered


class TestPersonasNoMarkdown:
    """Test that personas do not contain markdown formatting."""

    def test_tests_persona_no_markdown(self):
        """TESTS persona uses plain text, no markdown."""
        # Should not have markdown-specific syntax
        assert "**" not in personas.TESTS
        assert "##" not in personas.TESTS
        assert "```" not in personas.TESTS

    def test_implement_persona_no_markdown(self):
        """IMPLEMENT persona uses plain text, no markdown."""
        assert "**" not in personas.IMPLEMENT
        assert "##" not in personas.IMPLEMENT
        assert "```" not in personas.IMPLEMENT

    def test_repair_tests_persona_no_markdown(self):
        """REPAIR_TESTS persona uses plain text, no markdown."""
        assert "**" not in personas.REPAIR_TESTS
        assert "##" not in personas.REPAIR_TESTS
        assert "```" not in personas.REPAIR_TESTS

    def test_repair_implement_persona_no_markdown(self):
        """REPAIR_IMPLEMENT persona uses plain text, no markdown."""
        assert "**" not in personas.REPAIR_IMPLEMENT
        assert "##" not in personas.REPAIR_IMPLEMENT
        assert "```" not in personas.REPAIR_IMPLEMENT

    def test_repair_quality_persona_no_markdown(self):
        """REPAIR_QUALITY persona uses plain text, no markdown."""
        assert "**" not in personas.REPAIR_QUALITY
        assert "##" not in personas.REPAIR_QUALITY
        assert "```" not in personas.REPAIR_QUALITY

    def test_repair_ci_persona_no_markdown(self):
        """REPAIR_CI persona uses plain text, no markdown."""
        assert "**" not in personas.REPAIR_CI
        assert "##" not in personas.REPAIR_CI
        assert "```" not in personas.REPAIR_CI
