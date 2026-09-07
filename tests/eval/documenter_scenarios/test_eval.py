"""The stubbed documenter eval is deterministic, so it runs under pytest: every
fixture must pass its expectations (SC-006)."""
from __future__ import annotations

import pytest

from coordinare.eval.documenter_scenarios import run_fixture
from tests.eval.documenter_scenarios.fixtures import FIXTURES, Fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
async def test_fixture_meets_its_expectations(fixture: Fixture, tmp_path):
    score, _report = await run_fixture(fixture, live=False, root=tmp_path)
    assert score.passed, score.notes
