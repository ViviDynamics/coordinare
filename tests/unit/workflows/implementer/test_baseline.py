"""Tests for baseline detection (spec 167 FR-004)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from performer.workflows.implementer.baseline import NoTestRunner, detect_baseline
from performer.workflows.implementer.models import Baseline


@pytest.mark.asyncio
async def test_detect_baseline_with_test_command():
    """detect_baseline uses test command from score."""
    toolkit = MagicMock()
    toolkit.run_command = AsyncMock(return_value=MagicMock(
        output_excerpt="1 passed\n",
        exit_code=0,
    ))

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
    toolkit.run_command = mock_run_command

    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = {"command": "pytest"}

    await detect_baseline(toolkit, score)
    assert len(call_args) > 0
    assert call_args[0][0] == "pytest -rA"  # with_test_names adds the report flag
    assert call_args[0][1] == Path("/tmp/repo")


@pytest.mark.asyncio
async def test_detect_baseline_returns_baseline_model():
    """detect_baseline returns Baseline dataclass."""
    toolkit = MagicMock()
    toolkit.run_command = AsyncMock(return_value=MagicMock(
        output_excerpt="1 passed\n",
        exit_code=0,
    ))

    score = MagicMock()
    score.workspace_path = Path("/tmp/repo")
    score.local_test_gate_config = {"command": "pytest"}

    baseline = await detect_baseline(toolkit, score)
    assert isinstance(baseline, Baseline)
    assert baseline.stack == "pytest"  # stack_from_command reads the runner off the command
    assert baseline.detected_from == "score.local_test_gate_config"


def test_pytest_commands_gain_report_all_so_names_reach_the_checks():
    from performer.workflows.implementer.baseline import with_test_names

    assert with_test_names("pytest") == "pytest -rA"
    assert with_test_names("python -m pytest -q") == "python -m pytest -q -rA"
    assert with_test_names("pytest -rfE") == "pytest -rfE"
    assert with_test_names("bundle exec rspec") == "bundle exec rspec"


def test_runner_kind_prefers_the_command_over_a_language_stack():
    """Fifth live round: ci_detection reported stack "python", which matched no
    output parser, so a collection error produced no failure names."""
    from performer.workflows.implementer.baseline import runner_kind_for

    assert runner_kind_for("python", "pytest -rA") == "pytest"
    assert runner_kind_for("ruby", "bundle exec rspec") == "rspec"
    assert runner_kind_for("pytest", "pytest") == "pytest"
    assert runner_kind_for(None, "make test") == "unknown"
