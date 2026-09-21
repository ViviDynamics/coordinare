"""Tests for the 432 ruff-baseline ratchet script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ruff_baseline.py"

spec = importlib.util.spec_from_file_location("ruff_baseline", SCRIPT)
ruff_baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ruff_baseline)


class TestCompare:
    def test_an_increase_fails(self) -> None:
        increases, decreases = ruff_baseline.compare({"S101": 100}, {"S101": 101})
        assert increases == ["S101: 100 -> 101"]
        assert decreases == []

    def test_a_decrease_passes(self) -> None:
        increases, decreases = ruff_baseline.compare({"S101": 100}, {"S101": 99})
        assert increases == []
        assert decreases == ["S101: 100 -> 99"]

    def test_a_rule_absent_from_the_baseline_is_an_increase(self) -> None:
        """A rule reaching the tree after the snapshot is new debt, not free."""
        increases, _ = ruff_baseline.compare({}, {"PLR0913": 5})
        assert increases == ["PLR0913: 0 -> 5"]

    def test_a_rule_that_reaches_zero_passes_and_can_be_lowered(self) -> None:
        increases, decreases = ruff_baseline.compare({"PERF401": 44}, {"PERF401": 0})
        assert increases == []
        assert decreases == ["PERF401: 44 -> 0"]

    def test_equal_counts_hold(self) -> None:
        increases, decreases = ruff_baseline.compare({"S101": 10, "T20": 3}, {"S101": 10, "T20": 3})
        assert increases == []
        assert decreases == []

    def test_unrelated_rules_are_independent(self) -> None:
        increases, decreases = ruff_baseline.compare(
            {"S101": 10, "T20": 3}, {"S101": 12, "T20": 2},
        )
        assert increases == ["S101: 10 -> 12"]
        assert decreases == ["T20: 3 -> 2"]


class TestBaselineFile:
    def test_committed_baseline_covers_the_debt_families(self) -> None:
        """The snapshot exists and carries counts for every debt family.

        A family that reaches zero is promoted into the normal ruff select
        (the T201/S607 mechanic) and legitimately vanishes from the
        snapshot; the families listed here are the ones still carrying
        debt. Extend this tuple, never delete from it, until the family
        itself is promoted.
        """
        doc = ruff_baseline.json.loads((SCRIPT.parent / "ruff-baseline.json").read_text())
        assert doc["comment"].startswith("432 ratchet")
        rules = doc["rules"]
        for family in ("PERF401", "PLR", "TRY", "ASYNC", "S", "PTH", "PLC"):
            assert any(code.startswith(family) for code in rules), family
        assert all(count > 0 for count in rules.values())
