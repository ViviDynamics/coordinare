"""The stubbed closer eval is deterministic, so it runs under pytest (SC-005)."""
from __future__ import annotations

import pytest

from coordinare.eval.closer_scenarios import run_fixture
from tests.eval.closer_scenarios.fixtures import FIXTURES, Fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
async def test_fixture_meets_its_expectations(fixture: Fixture):
    score, _report = await run_fixture(fixture, live=False)
    assert score.passed, score.notes
