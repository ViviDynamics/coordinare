"""The stubbed scenario runners (164 to 167) are CLIs a human runs; exercise
their stubbed path under pytest so a broken runner is caught in CI and so
the coverage gate measures them (they tipped the total under 90 percent on
spec 167's branch)."""
from __future__ import annotations

import asyncio

import pytest

from coordinare.eval import architect_scenarios, assessor_scenarios, implementer_scenarios


@pytest.mark.parametrize("module", [architect_scenarios, assessor_scenarios, implementer_scenarios])
def test_stubbed_runner_passes_every_fixture(module):
    scores = asyncio.run(module.run_all(live=False, only=None))
    assert scores, "the runner produced no results"
    for score in scores:
        passed = score.passed if hasattr(score, "passed") else score.get("passed")
        assert passed, getattr(score, "notes", None) or score


@pytest.mark.parametrize("module", [architect_scenarios, assessor_scenarios, implementer_scenarios])
def test_runner_main_exits_zero_stubbed(module, capsys):
    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_runner_main_only_selects_one_fixture(capsys):
    assert implementer_scenarios.main(["--only", "chore"]) == 0
    out = capsys.readouterr().out
    assert "chore" in out and "two_milestones" not in out


def test_qa_runner_main_exits_zero_stubbed_with_one_repeat(capsys):
    from coordinare.eval import qa_scenarios

    assert qa_scenarios.main(["--repeats", "1"]) == 0
    assert capsys.readouterr().out.strip()
