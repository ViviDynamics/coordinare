"""The existing advisory benchmark lane must execute native budgets against its base."""
from __future__ import annotations

from pathlib import Path

import yaml


def test_native_benchmark_lane_has_pinned_base_and_runs_both_suites() -> None:
    workflow = yaml.safe_load(Path('.github/workflows/pr-ci.yml').read_text())
    steps = workflow['jobs']['benchmark']['steps']
    baseline = next(step for step in steps if step.get('name') == 'Checkout benchmark baseline')
    assert baseline['with']['ref'] == '${{ github.event.pull_request.base.sha }}'
    assert baseline['with']['persist-credentials'] is False
    run = next(step for step in steps if step.get('name') == 'Run performance benchmarks (T067)')
    assert 'tests/benchmarks/test_376_agent_runtime.py' in run['run']
    assert 'tests/benchmarks/test_performer_perf.py' in run['run']
    assert run['env']['COORDINARE_BENCHMARK_BASELINE'] == baseline['with']['path']
