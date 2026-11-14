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


def test_full_suite_checkouts_keep_the_baseline_parent() -> None:
    for filename in ('pr-ci.yml', 'main-branch-build.yml'):
        workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
        for job in ('test', 'coverage-shard'):
            checkout = next(step for step in workflow['jobs'][job]['steps'] if step.get('name') == 'Checkout')
            assert checkout.get('with', {}).get('fetch-depth') == 2


def test_offline_baseline_is_pinned_source_not_the_working_tree(tmp_path, monkeypatch) -> None:
    import subprocess

    from tests.benchmarks.test_376_agent_runtime import baseline_source

    repo = tmp_path / 'repository'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    source = repo / 'src/coordinare/daemon.py'
    source.parent.mkdir(parents=True)
    source.write_text('BASE = True\n')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    parent = git('rev-parse', 'HEAD')
    source.write_text('CANDIDATE = True\n')
    git('commit', '-qam', 'candidate')
    monkeypatch.delenv('COORDINARE_BENCHMARK_BASELINE', raising=False)
    baseline, revision = baseline_source(repo, tmp_path / 'extracted')
    assert revision == parent
    assert (baseline / 'coordinare/daemon.py').read_text() == 'BASE = True\n'
    assert source.read_text() == 'CANDIDATE = True\n'
