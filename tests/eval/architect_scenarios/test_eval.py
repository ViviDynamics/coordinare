"""The stubbed architect eval is deterministic, so it runs under pytest: every
fixture must pass its expectations (SC-004)."""
from __future__ import annotations

import pytest

from coordinare.eval.architect_scenarios import run_fixture
from tests.eval.architect_scenarios.fixtures import FIXTURES


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
async def test_fixture_meets_its_expectations(fixture, tmp_path):
    score, report = await run_fixture(fixture, live=False, root=tmp_path)
    assert score.passed, score.notes
    assert report["write_free_check"]["passed"] is True


@pytest.mark.asyncio
async def test_no_fixture_lets_a_refused_command_run(tmp_path):
    for fixture in FIXTURES:
        score, _report = await run_fixture(fixture, live=False, root=tmp_path)
        assert score.checks["refusals_refused"], fixture.name


def test_scoring_flags_a_documenter_decision_that_contradicts_the_fixture():
    """The scorer's documenter check must be able to fail: a trivial card whose
    blueprint grew a documentation topic would dispatch a documenter the
    fixture says it must not."""
    from tests.eval.architect_scenarios.fixtures import FIXTURES
    from tests.eval.architect_scenarios.scoring import score_run
    from tests.eval.architect_scenarios.stub_model import (
        stub_model_for,  # noqa: F401  (same module the eval uses)
    )

    trivial = next(f for f in FIXTURES if f.name == "trivial")
    bp = {
        "summary": "s", "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}], "modules": [],
        "data_model": {"changes": []}, "interfaces": [], "risks": [],
        "criteria": [{"surface": "/", "action": "a", "expected": "e", "kind": "functional"}],
        "docs": [{"topic": "t", "location": "docs/x.md", "say": "s"}], "blueprint_hash": "h",
    }
    report = {"blueprint": bp, "size": "small", "write_free_check": {"passed": True, "refused_commands": 9}}
    score = score_run(trivial, report, ["ls"])
    assert score.checks["documenter_decision"] is False
    assert score.checks["docs"] is False
