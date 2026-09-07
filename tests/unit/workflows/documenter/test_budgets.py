"""Tests for documenter budgets (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.budgets import DocumenterBudgets


class TestDocumenterBudgetsFromEnv:
    """Test budget parsing from environment."""

    def test_defaults(self):
        """Default budgets match spec."""
        budgets = DocumenterBudgets.from_env({})

        assert budgets.plan_cap == 8
        assert budgets.gather_max_commands == 6
        assert budgets.gather_max_output_chars == 4000
        assert budgets.page_max_chars == 12000

    def test_env_overrides(self):
        """Environment variables override defaults."""
        env = {
            "DOC_PLAN_CAP": "4",
            "DOC_GATHER_MAX_COMMANDS": "3",
            "DOC_GATHER_MAX_OUTPUT_CHARS": "2000",
            "DOC_PAGE_MAX_CHARS": "8000",
        }
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.plan_cap == 4
        assert budgets.gather_max_commands == 3
        assert budgets.gather_max_output_chars == 2000
        assert budgets.page_max_chars == 8000

    def test_plan_cap_never_above_8(self):
        """Plan cap cannot exceed 8."""
        env = {"DOC_PLAN_CAP": "10"}
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.plan_cap == 8

    def test_positive_int_parsing(self):
        """Values must be positive integers."""
        # Non-integer values should use defaults
        env = {"DOC_GATHER_MAX_COMMANDS": "not_a_number"}
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.gather_max_commands == 6  # Default

    def test_negative_values_fallback_to_defaults(self):
        """Negative values fall back to defaults."""
        env = {"DOC_GATHER_MAX_OUTPUT_CHARS": "-1"}
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.gather_max_output_chars == 4000

    def test_zero_values_fallback_to_defaults(self):
        """Zero values fall back to defaults."""
        env = {"DOC_PAGE_MAX_CHARS": "0"}
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.page_max_chars == 12000

    def test_partial_env(self):
        """Some env vars set, others use defaults."""
        env = {"DOC_PLAN_CAP": "6"}
        budgets = DocumenterBudgets.from_env(env)

        assert budgets.plan_cap == 6
        assert budgets.gather_max_commands == 6  # Default
        assert budgets.gather_max_output_chars == 4000  # Default
