"""Unit tests for closer workflow budgets (spec 172)."""
from __future__ import annotations

import pytest
from performer.workflows.closer.budgets import CloserBudgets


class TestCloserBudgets:
    """CloserBudgets initialization and environment parsing."""

    def test_default_budgets(self) -> None:
        b = CloserBudgets()
        assert b.max_threads_per_call == 20
        assert b.max_pages == 5

    def test_custom_budgets(self) -> None:
        b = CloserBudgets(max_threads_per_call=10, max_pages=3)
        assert b.max_threads_per_call == 10
        assert b.max_pages == 3

    def test_max_threads_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            CloserBudgets(max_threads_per_call=0)

    def test_max_pages_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            CloserBudgets(max_pages=0)

    def test_from_env_defaults(self) -> None:
        b = CloserBudgets.from_env(None)
        assert b.max_threads_per_call == 20
        assert b.max_pages == 5

    def test_from_env_parses_int(self) -> None:
        env = {"CLOSER_MAX_THREADS_PER_CALL": "15", "CLOSER_MAX_PAGES": "2"}
        b = CloserBudgets.from_env(env)
        assert b.max_threads_per_call == 15
        assert b.max_pages == 2

    def test_from_env_invalid_int_uses_default(self) -> None:
        env = {"CLOSER_MAX_THREADS_PER_CALL": "not_a_number"}
        b = CloserBudgets.from_env(env)
        assert b.max_threads_per_call == 20

    def test_from_env_enforces_min_value(self) -> None:
        env = {"CLOSER_MAX_THREADS_PER_CALL": "0"}
        with pytest.raises(ValueError):
            CloserBudgets.from_env(env)

    def test_from_env_empty_dict(self) -> None:
        b = CloserBudgets.from_env({})
        assert b.max_threads_per_call == 20
        assert b.max_pages == 5
