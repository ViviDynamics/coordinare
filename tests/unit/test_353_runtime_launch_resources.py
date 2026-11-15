"""Private carrier launches apply reviewed bounds without gaining privileges."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coordinare_service_inference.environment.runtime import RuntimePolicy

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import performer_lifecycle
from coordinare.services.docker_runtime import DockerRuntime
from tests.unit.test_353_runtime_identity import policy_data


@pytest.mark.asyncio
@pytest.mark.parametrize('egress', [False, True])
async def test_private_launch_applies_limits_or_refuses_privilege_conflict(monkeypatch, egress):
    calls = []
    async def docker(*args, **kwargs):
        calls.append(args)
        return 0, ('owned' if args[0] == 'run' else '127.0.0.1:12345'), ''
    monkeypatch.setattr(performer_lifecycle, '_run_docker', docker)
    config = PerformerEndpointConfig(id='p', roles=['implementer'], mode='ephemeral', image='performer:latest',
        egress_allowlist=['example.org'] if egress else [])
    policy = RuntimePolicy.model_validate(policy_data())
    if egress:
        with pytest.raises(performer_lifecycle.ContainerStartError, match='privilege'):
            await DockerRuntime().start_ephemeral(config, runtime_policy=policy)
        assert calls == []
    else:
        await DockerRuntime().start_ephemeral(config, runtime_policy=policy)
        args = next(args for args in calls if args[0] == 'run')
        for flag, value in [('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges'),
                            ('--cpus', '2'), ('--memory', '4096m'), ('--memory-swap', '4096m'), ('--pids-limit', '512')]:
            assert args[args.index(flag)+1] == value
        assert '--cap-add' not in args


@pytest.mark.asyncio
async def test_runtime_forwards_policy_without_mutating_endpoint(monkeypatch):
    start = AsyncMock(return_value=SimpleNamespace(container_id='owned', endpoint='http://localhost:8088'))
    monkeypatch.setattr(performer_lifecycle, 'start_ephemeral', start)
    config = PerformerEndpointConfig(id='p', roles=['implementer'], mode='ephemeral', image='performer:latest')
    policy = RuntimePolicy.model_validate(policy_data())
    await DockerRuntime().start_ephemeral(config, runtime_policy=policy)
    assert start.await_args.kwargs['runtime_policy'] == policy
    assert config.image == 'performer:latest'


@pytest.mark.parametrize('fault', [None, 'digest', 'review'])
def test_launch_policy_requires_exact_independently_reviewed_contract(tmp_path, fault):
    from agent.performer.tests.test_353_environment_runtime import private_score

    from coordinare.services.environment_runtime import launch_runtime_policy

    score, manifest, _ = private_score(tmp_path)
    metadata = {'environment_manifest': score.environment_manifest,
                'environment_assessment': score.environment_assessment,
                'environment_manifest_digest': score.environment_manifest_digest}
    if fault == 'digest':
        metadata['environment_manifest_digest'] = '0'*64
    elif fault == 'review':
        metadata['environment_assessment'] = {**score.environment_assessment, 'verdict': 'reject'}
    if fault:
        with pytest.raises(ValueError):
            launch_runtime_policy(metadata)
    else:
        assert launch_runtime_policy(metadata) == manifest.runtime.policy
    assert launch_runtime_policy({}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('supported', [True, False])
async def test_dispatch_applies_policy_before_start_or_refuses_old_runtime(tmp_path, monkeypatch, supported):
    from agent.performer.tests.test_353_environment_runtime import private_score

    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.performer_runtime import StartedPerformer

    score, manifest, _ = private_score(tmp_path)
    launched = []
    async def start(config, *, extra_labels=None, backend=None, runtime_policy=None):
        launched.append(runtime_policy)
        return StartedPerformer('owned', 'http://localhost:8088')
    async def old_start(config, *, extra_labels=None, backend=None):
        launched.append(None)
        return StartedPerformer('owned', 'http://localhost:8088')
    runtime = SimpleNamespace(start_ephemeral=start if supported else old_start, stop=AsyncMock())
    config = PerformerEndpointConfig(id='p', roles=['implementer'], mode='ephemeral', image='performer:latest')
    instance = HTTPPerformerService(config, runtime=runtime)
    monkeypatch.setattr(performer_lifecycle, 'wait_ready', AsyncMock(side_effect=performer_lifecycle.ReadinessTimeoutError('test boundary')))
    try:
        response = await instance.dispatch_card({'id': 'card', 'role': 'implementer',
            'environment_manifest': score.environment_manifest, 'environment_assessment': score.environment_assessment,
            'environment_manifest_digest': score.environment_manifest_digest})
        assert response['status'] == 'error'
        if supported:
            assert launched == [manifest.runtime.policy]
            runtime.stop.assert_awaited_once_with('owned')
        else:
            assert launched == []
            assert 'private runtime' in response['reason']
    finally:
        await instance.aclose()
