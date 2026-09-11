"""Tests for baseline detection (spec 167 FR-004)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from performer.workflows.implementer.baseline import NoTestRunner, detect_baseline
from performer.workflows.implementer.models import Baseline


def _observation_for(exit_code: int, output: str):
    """Stand in for the model reading the runner output (#365).

    Mirrors what a model would say about these fixtures' output so the
    assertions under test -- which are about baselines and env holds, not about
    how the output was read -- keep their original meaning.
    """
    from performer.workflows.implementer.observe import TestObservation

    if exit_code == 0:
        return TestObservation(outcome="all_passed", passed=["example"])
    return TestObservation(outcome="assertion_failure", failed=["example"], summary=output[:200])



@pytest.mark.asyncio
async def test_detect_baseline_with_test_command():
    """detect_baseline uses test command from score."""
    toolkit = MagicMock()
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, ''))
    toolkit.run_command = AsyncMock(return_value=MagicMock(
        output_excerpt="1 passed\n",
        exit_code=0,
    ))
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, "1 passed"))

    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = {"command": "pytest"}

    baseline = await detect_baseline(toolkit, score)
    assert isinstance(baseline, Baseline)
    assert baseline.detected_from == "score.local_test_gate_config"


@pytest.mark.asyncio
async def test_detect_baseline_raises_when_no_test_command():
    """detect_baseline raises NoTestRunner when test command not found."""
    toolkit = MagicMock()
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, ''))
    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = None
    score.test_command = None

    with pytest.raises(NoTestRunner):
        await detect_baseline(toolkit, score)


@pytest.mark.asyncio
async def test_detect_baseline_uses_toolkit_run_command():
    """detect_baseline runs test command via toolkit."""
    call_args = []

    async def mock_run_command(cmd, cwd=None, timeout_s=None):
        call_args.append((cmd, cwd, timeout_s))
        return MagicMock(output_excerpt="", exit_code=0)

    toolkit = MagicMock()
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, ''))
    toolkit.run_command = mock_run_command

    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = {"command": "pytest"}

    await detect_baseline(toolkit, score)
    assert len(call_args) > 0
    # 365: the command is passed through as the repository states it. There is no
    # per-runner flag injection left -- the model reads whatever format the runner
    # emits, which is what makes this work on a stack coordinare has never seen.
    assert call_args[0][0] == "pytest"
    assert call_args[0][1] == Path("/tmp/repo")


@pytest.mark.asyncio
async def test_detect_baseline_returns_baseline_model():
    """detect_baseline returns Baseline dataclass."""
    toolkit = MagicMock()
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, ''))
    toolkit.run_command = AsyncMock(return_value=MagicMock(
        output_excerpt="1 passed\n",
        exit_code=0,
    ))
    toolkit.call_model = AsyncMock(return_value=_observation_for(0, "1 passed"))

    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = {"command": "pytest"}

    baseline = await detect_baseline(toolkit, score)
    assert isinstance(baseline, Baseline)
    # 365: no runner kind is derived any more; nothing downstream parses by runner.
    assert baseline.stack == ""
    assert baseline.detected_from == "score.local_test_gate_config"



