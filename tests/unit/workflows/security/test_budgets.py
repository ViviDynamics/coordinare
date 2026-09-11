"""Unit tests for security workflow budgets (spec 170)."""

from __future__ import annotations

import pytest
from performer.workflows.security.budgets import SecurityBudgets


class TestSecurityBudgets:
    """Test budget loading from environment."""

    def test_budgets_from_env_defaults(self):
        """Defaults are applied when env vars are absent."""
        budgets = SecurityBudgets.from_env({})
        assert budgets.scan_timeout_s == 120
        assert budgets.survey_max_commands == 12
        assert budgets.survey_max_output_chars == 4000
        assert budgets.max_findings == 30

    def test_budgets_from_env_overrides(self):
        """Environment variables override defaults."""
        env = {
            "SECURITY_SCAN_TIMEOUT_S": "180",
            "SECURITY_SURVEY_MAX_COMMANDS": "10",
            "SECURITY_SURVEY_MAX_OUTPUT_CHARS": "5000",
            "SECURITY_MAX_FINDINGS": "20",
        }
        budgets = SecurityBudgets.from_env(env)
        assert budgets.scan_timeout_s == 180
        assert budgets.survey_max_commands == 10
        assert budgets.survey_max_output_chars == 5000
        assert budgets.max_findings == 20

    def test_budgets_max_findings_capped_at_30(self):
        """max_findings never exceeds 30."""
        env = {"SECURITY_MAX_FINDINGS": "100"}
        budgets = SecurityBudgets.from_env(env)
        assert budgets.max_findings == 30

    def test_budgets_invalid_scan_timeout_fallback(self):
        """Non-numeric scan timeout falls back to default."""
        env = {"SECURITY_SCAN_TIMEOUT_S": "not_a_number"}
        budgets = SecurityBudgets.from_env(env)
        assert budgets.scan_timeout_s == 120

    def test_budgets_zero_timeout_not_allowed(self):
        """Scan timeout must be positive."""
        env = {"SECURITY_SCAN_TIMEOUT_S": "0"}
        with pytest.raises(ValueError):
            SecurityBudgets.from_env(env)

    def test_budgets_negative_timeout_not_allowed(self):
        """Scan timeout must be positive."""
        env = {"SECURITY_SCAN_TIMEOUT_S": "-1"}
        with pytest.raises(ValueError):
            SecurityBudgets.from_env(env)

    def test_budgets_survey_budget(self):
        """survey_budget() returns an architect SurveyBudget."""
        budgets = SecurityBudgets.from_env({})
        survey_budget = budgets.survey_budget()
        assert survey_budget.max_commands == 12
        assert survey_budget.max_output_chars == 4000
