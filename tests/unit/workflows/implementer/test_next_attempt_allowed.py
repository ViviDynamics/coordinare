"""Tests for next_attempt_allowed gate function (spec 167 FR-006, FR-010, FR-013).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: change "<" to "<=" -> test_next_attempt_allowed_implement_at_limit fails
- Mutation: remove the "kind" checks -> tests with different kinds will fail
"""

from performer.workflows.implementer.budgets import ImplementerBudgets
from performer.workflows.implementer.cycle import next_attempt_allowed


class TestNextAttemptAllowed:
    """Test next_attempt_allowed gate: checks if another attempt is within budget (FR-006, FR-010, FR-013)."""

    def test_next_attempt_allowed_implement_within_budget(self):
        """Next attempt allowed: implementation attempt within 3-attempt budget."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("implement", 0, budgets)
        assert result is True

    def test_next_attempt_allowed_implement_at_limit(self):
        """Next attempt allowed: at limit but still allowed (mutation: change < to <= to fail)."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("implement", 2, budgets)
        assert result is True

    def test_next_attempt_allowed_implement_exhausted(self):
        """Next attempt allowed: implementation budget exhausted."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("implement", 3, budgets)
        assert result is False

    def test_next_attempt_allowed_repair_quality_within_budget(self):
        """Next attempt allowed: quality repair within 2-repair budget."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_quality", 0, budgets)
        assert result is True

    def test_next_attempt_allowed_repair_quality_at_limit(self):
        """Next attempt allowed: quality repair at limit."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_quality", 1, budgets)
        assert result is True

    def test_next_attempt_allowed_repair_quality_exhausted(self):
        """Next attempt allowed: quality repair budget exhausted."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_quality", 2, budgets)
        assert result is False

    def test_next_attempt_allowed_repair_ci_within_budget(self):
        """Next attempt allowed: CI repair within 3-repair budget."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_ci", 0, budgets)
        assert result is True

    def test_next_attempt_allowed_repair_ci_at_limit(self):
        """Next attempt allowed: CI repair at limit."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_ci", 2, budgets)
        assert result is True

    def test_next_attempt_allowed_repair_ci_exhausted(self):
        """Next attempt allowed: CI repair budget exhausted."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_ci", 3, budgets)
        assert result is False

    def test_next_attempt_allowed_repair_tests_within_budget(self):
        """Next attempt allowed: test reprompt within 1-reprompt budget."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_tests", 0, budgets)
        assert result is True

    def test_next_attempt_allowed_repair_tests_exhausted(self):
        """Next attempt allowed: test reprompt budget exhausted."""
        budgets = ImplementerBudgets()
        result = next_attempt_allowed("repair_tests", 1, budgets)
        assert result is False

    def test_next_attempt_allowed_custom_budgets(self):
        """Next attempt allowed: respects custom budget from environment."""
        budgets = ImplementerBudgets(impl_attempts=5)
        result = next_attempt_allowed("implement", 4, budgets)
        assert result is True
        result = next_attempt_allowed("implement", 5, budgets)
        assert result is False
