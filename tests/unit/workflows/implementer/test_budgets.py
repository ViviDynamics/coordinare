"""Tests for implementer workflow budgets (spec 167 FR-016)."""
from __future__ import annotations

import pytest
from performer.workflows.implementer.budgets import ImplementerBudgets


def test_budgets_default_values():
    """Default budgets match FR-016 spec."""
    budgets = ImplementerBudgets()
    assert budgets.turn_timeout_s == 1200
    assert budgets.tests_reprompts == 1
    assert budgets.impl_attempts == 3
    assert budgets.quality_repairs == 2
    assert budgets.ci_repairs == 3
    assert budgets.ci_wait_s == 1800
    assert budgets.quality_commands == ()


def test_budgets_from_env_overrides_defaults():
    """from_env reads IMPL_* env vars and overrides defaults."""
    env = {
        "IMPL_TURN_TIMEOUT_S": "600",
        "IMPL_TESTS_REPROMPTS": "2",
        "IMPL_ATTEMPTS": "5",
        "IMPL_QUALITY_REPAIRS": "3",
        "IMPL_CI_REPAIRS": "4",
        "IMPL_CI_WAIT_S": "2400",
    }
    budgets = ImplementerBudgets.from_env(env)
    assert budgets.turn_timeout_s == 600
    assert budgets.tests_reprompts == 2
    assert budgets.impl_attempts == 5
    assert budgets.quality_repairs == 3
    assert budgets.ci_repairs == 4
    assert budgets.ci_wait_s == 2400


def test_budgets_from_env_parses_quality_commands():
    """QUALITY_COMMANDS is split on newlines, stripped, blanks dropped."""
    env = {
        "QUALITY_COMMANDS": "ruff check .\nmypycheck\n\n  eslint --fix  \n",
    }
    budgets = ImplementerBudgets.from_env(env)
    assert budgets.quality_commands == ("ruff check .", "mypycheck", "eslint --fix")


def test_budgets_from_env_invalid_int_falls_back():
    """Invalid (non-numeric, negative) values fall back to defaults."""
    env = {
        "IMPL_TURN_TIMEOUT_S": "not_a_number",
        "IMPL_ATTEMPTS": "-5",
        "IMPL_QUALITY_REPAIRS": "0",
    }
    budgets = ImplementerBudgets.from_env(env)
    assert budgets.turn_timeout_s == 1200  # default
    assert budgets.impl_attempts == 3  # default
    assert budgets.quality_repairs == 2  # default


def test_budgets_from_env_empty_dict():
    """Empty env dict returns defaults."""
    budgets = ImplementerBudgets.from_env({})
    assert budgets.turn_timeout_s == 1200
    assert budgets.quality_commands == ()


def test_budgets_from_env_none():
    """None env returns defaults."""
    budgets = ImplementerBudgets.from_env(None)
    assert budgets.turn_timeout_s == 1200
    assert budgets.quality_commands == ()


def test_budgets_frozen():
    """ImplementerBudgets is frozen and immutable."""
    budgets = ImplementerBudgets()
    with pytest.raises(AttributeError):
        budgets.turn_timeout_s = 600


def test_budgets_quality_commands_is_tuple():
    """quality_commands is a tuple, not a list."""
    budgets = ImplementerBudgets()
    assert isinstance(budgets.quality_commands, tuple)
