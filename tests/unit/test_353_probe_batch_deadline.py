"""Private probe batches allow fresh guests without renewing the attempt budget."""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_probes import ProbeCommandResult, ProbePlan, ProbeReport
from tests.unit.test_353_discovery_repair import PATH, SOURCE
from tests.unit.test_353_environment_probes import plan, report_data
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_execution_source_pipeline import bundle_fixture
from tests.unit.test_353_runtime_probe_integration import private_plan


def bound_plan(tmp_path, private):
    bundle = bundle_fixture(tmp_path, {PATH: (SOURCE.encode(), '100644')})
    selected = (private_plan() if private else plan()).model_copy(update={
        'inputs': {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()}, 'execution_source_digest': bundle.identity.digest()})
    return selected, bundle


@pytest.mark.asyncio
@pytest.mark.parametrize(('private', 'seconds', 'expected'), [(True, 3600, 1800), (True, 45, 45), (False, 3600, 120)])
async def test_owned_probe_deadline_is_context_bound_and_capped_by_original_attempt(tmp_path, monkeypatch, private, seconds, expected):
    from coordinare.services import environment_probe_service as service
    from coordinare.services.environment_repair import begin_attempt, save_attempt

    selected, bundle = bound_plan(tmp_path, private)
    cache = cache_fixture(tmp_path)
    limits = DiscoveryLimits(attempt_timeout_seconds=3600)
    budget = begin_attempt(cache, limits, outer_seconds=seconds)
    expiry = cache.environment_attempt['expires_at']
    budget.reserve_probe_batch()
    async def checkpoint():
        save_attempt(cache, budget)
    budget.checkpoint = checkpoint
    await budget.flush()
    async def fetch(path):
        return SOURCE
    before = time.monotonic()
    async def execute(p, **kwargs):
        assert before + expected - 1 <= kwargs['deadline'] <= time.monotonic() + expected
        assert kwargs['deadline'] <= budget.deadline
        data = report_data(p)
        data.update(image_ref=p.capabilities.image_ref, platform=p.capabilities.platform)
        return ProbeReport.model_validate(data)
    monkeypatch.setattr(service, 'execute_environment_probe', execute)
    await service.run_owned_probe(selected, context=service.ProbeServiceContext(cache, {}, fetch, budget, limits, bundle))
    assert cache.environment_attempt['expires_at'] == expiry
    assert cache.environment_attempt['probe_owner'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(('private', 'seconds', 'explicit', 'expected'), [
    (True, 3600, None, 1800), (True, 45, None, 45), (True, 3600, 60, 60), (True, 3600, 3600, 1800), (False, 3600, None, 120),
])
async def test_executor_default_deadline_matches_service_and_never_exceeds_attempt(tmp_path, monkeypatch, private, seconds, explicit, expected):
    from coordinare.services import environment_probe_executor as executor

    selected, bundle = bound_plan(tmp_path, private)
    budget = AttemptBudget(timeout_seconds=seconds)
    monkeypatch.setattr(executor, '_docker_identity', AsyncMock(return_value=(selected.capabilities.image_ref, selected.capabilities.platform)))
    before = time.monotonic()
    async def command(p, roots, command, deadline, **kwargs):
        assert before + expected - 1 <= deadline <= time.monotonic() + expected
        assert deadline <= budget.deadline
        return ProbeCommandResult(id=command['id'], status='completed', exit_code=0, output='', truncated=False)
    monkeypatch.setattr(executor, '_command_container', command)
    await executor.execute_environment_probe(selected, state={'probe_backend': 'docker'}, source_bundle=bundle,
        budget=budget, deadline=None if explicit is None else before+explicit)


@pytest.mark.asyncio
async def test_eleven_private_observations_share_one_thirty_minute_deadline(tmp_path, monkeypatch):
    from coordinare.services import environment_probe_executor as executor

    selected, bundle = bound_plan(tmp_path, True)
    raw = selected.model_dump(mode='json')
    raw['request']['commands'] = [{**raw['request']['commands'][0], 'id': f'observe-{i}'} for i in range(11)]
    raw['request']['citations'] = {c['id']: [PATH] for c in raw['request']['commands']}
    raw['request']['questions'] = {c['id']: 'Is the required prerequisite available?' for c in raw['request']['commands']}
    selected = ProbePlan.model_validate(raw)
    budget = AttemptBudget(timeout_seconds=3600)
    clock = [time.monotonic()]
    monkeypatch.setattr(executor, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(executor, '_docker_identity', AsyncMock(return_value=(selected.capabilities.image_ref, selected.capabilities.platform)))
    deadlines, scratch_paths = [], []
    async def command(p, roots, command, deadline, *, scratch):
        assert scratch.is_dir() and not list(scratch.iterdir())
        (scratch/'owned').write_text('isolated command')
        scratch_paths.append(scratch)
        deadlines.append(deadline)
        clock[0] += 90  # Fresh guest startup + observation + teardown, without real waiting.
        assert clock[0] < deadline
        return ProbeCommandResult(id=command['id'], status='completed', exit_code=0, output='available', truncated=False)
    monkeypatch.setattr(executor, '_command_container', command)
    report = await executor.execute_environment_probe(selected, state={'probe_backend': 'docker'}, source_bundle=bundle, budget=budget)
    assert len(report.results) == 11 and report.cleanup_confirmed
    assert len(set(deadlines)) == 1 and deadlines[0] <= budget.deadline
    assert len(set(scratch_paths)) == 11 and all(not path.exists() for path in scratch_paths)
    assert all(c.timeout_seconds == 30 for c in selected.request.commands)
