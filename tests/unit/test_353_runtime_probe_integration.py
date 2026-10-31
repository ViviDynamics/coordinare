"""Platform observations choose explicit execution context without changing readiness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinare.services.environment_probe_executor import probe_container_argv
from coordinare.services.environment_probes import ProbePlan, ProbeRequest
from tests.unit.test_353_environment_probes import plan
from tests.unit.test_353_runtime_identity import policy_data


def private_plan():
    data = plan().model_dump(mode='json')
    data['request']['execution_context'] = 'private_service'
    data['capabilities'].update(schema_version=2, runtime=policy_data())
    data['capabilities']['image_ref'] = policy_data()['identity']['carrier_image']
    data['capabilities']['platform'] = policy_data()['identity']['carrier_platform']
    return ProbePlan.model_validate(data)


def test_private_preflight_requires_an_explicit_platform_and_binds_plan_identity():
    data = plan().model_dump(mode='json')
    data['request']['execution_context'] = 'private_service'
    with pytest.raises(ValueError, match='runtime'):
        ProbePlan.model_validate(data)
    selected = private_plan()
    assert selected.request.execution_context == 'private_service'
    assert selected.model_dump()['request']['execution_context'] == 'private_service'
    assert selected.digest() != selected.model_copy(update={'request': plan().request}).digest()


def test_outer_probe_wire_identity_keeps_legacy_serialization():
    request = plan().request
    raw = request.model_dump(mode='json')
    assert 'execution_context' not in raw
    assert ProbeRequest.model_validate({**raw, 'execution_context': 'outer'}).model_dump(mode='json') == raw


def test_private_probe_container_keeps_readonly_inputs_and_uses_policy_limits(tmp_path):
    args = probe_container_argv(private_plan(), tmp_path/'repo', tmp_path/'cache', scratch=tmp_path/'scratch')
    assert '--network=none' in args and '--read-only' in args
    assert '--cap-drop=ALL' in args and '--security-opt=no-new-privileges' in args
    assert '--memory=4096m' in args and '--memory-swap=4096m' in args and '--cpus=2' in args
    mounts = [args[index+1] for index, item in enumerate(args) if item == '--mount']
    assert any('target=/repo,readonly' in item for item in mounts)
    assert any('target=/cache,readonly' in item for item in mounts)
    assert any('target=/scratch' in item and 'readonly' not in item for item in mounts)
    assert not any('docker.sock' in item or 'privileged' in item for item in args)
    assert probe_container_argv(plan(), tmp_path/'repo', tmp_path/'cache')[-1] == 'import time;time.sleep(600)'


def test_probe_trusted_runtime_activation_preserves_wrapper_and_is_not_ambient(monkeypatch):
    from coordinare_service_inference.environment.observation import command_environment

    monkeypatch.setenv('DOCKER_HOST', 'ambient-untrusted')
    command = {'env': {'PATH': '/declared/bin'}}
    assert 'DOCKER_HOST' not in command_environment(command)
    env = command_environment(command, runtime_env={'DOCKER_HOST': 'ssh://root@127.0.0.1:2222',
        'COORDINARE_ENV_RUNTIME_BIN': '/private/job/bin'})
    assert env['DOCKER_HOST'] == 'ssh://root@127.0.0.1:2222'
    assert env['PATH'] == '/private/job/bin:/declared/bin'


@pytest.mark.asyncio
@pytest.mark.parametrize('clean', [True, False])
async def test_preflight_uses_platform_only_runtime_and_requires_full_teardown(tmp_path, monkeypatch, clean):
    import sys
    import time
    from types import SimpleNamespace

    from coordinare_service_inference.environment import runtime_observation as runner

    observed = {}
    class Lease:
        deadline = time.monotonic()+10
        def __init__(self):
            self.activation_env = {'DOCKER_HOST': 'ssh://root@127.0.0.1:2222', 'COORDINARE_ENV_RUNTIME_BIN': '/private/job/bin'}
        def check_bounds(self):
            pass
        async def close(self, *, deadline):
            observed['closed'] = True
            return SimpleNamespace(confirmed=clean, clean_shutdown=True, bounds_respected=True)
    async def open_runtime(spec, **kwargs):
        observed.update(kwargs)
        assert spec.policy.identity.guest_base_sha256 == 'b'*64
        assert not spec.endpoints
        return Lease()
    monkeypatch.setattr(runner, 'open_runtime', open_runtime)
    data = {'commands': [{'id': 'probe', 'argv': [sys.executable, '-c',
        'import os,sys;print(os.environ["DOCKER_HOST"]);sys.exit(4)'], 'cwd': str(tmp_path), 'env': {}, 'timeout_seconds': 2}],
        'seconds': 5, 'expires_at': time.time()+5, 'runtime': policy_data(), 'execution_source_digest': 'a'*64}
    if clean:
        result = await runner.observe_private(data, repo=tmp_path/'repo', cache=tmp_path/'cache', scratch=tmp_path)
        assert result[0]['status'] == 'completed' and result[0]['exit_code'] == 4
        assert result[0]['output'].strip() == 'ssh://root@127.0.0.1:2222'
    else:
        with pytest.raises(RuntimeError, match='teardown'):
            await runner.observe_private(data, repo=tmp_path/'repo', cache=tmp_path/'cache', scratch=tmp_path)
    assert observed['purpose'] == 'preflight' and observed['artifacts'] is None
    assert observed['install'] is False and observed['source_digest'] == 'a'*64
    assert observed['closed'] is True
    assert observed['deadline'] <= time.monotonic()+5
    assert not list(tmp_path.glob('coordinare-project-*'))


@pytest.mark.asyncio
async def test_private_observation_expired_in_transport_cannot_start_guest(tmp_path, monkeypatch):
    import time
    from unittest.mock import AsyncMock

    from coordinare_service_inference.environment import runtime_observation as runner

    opened = AsyncMock(side_effect=AssertionError('expired plan started a guest'))
    monkeypatch.setattr(runner, 'open_runtime', opened)
    with pytest.raises(TimeoutError):
        await runner.observe_private({'commands': [{'id': 'probe'}], 'seconds': 120,
            'expires_at': time.time()-1, 'runtime': policy_data(), 'execution_source_digest': 'a'*64}, scratch=tmp_path)
    opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_probe_transport_preserves_source_and_expiry_and_resets_scratch(tmp_path, monkeypatch):
    import hashlib
    import time
    from unittest.mock import AsyncMock

    from coordinare.services import environment_probe_executor as executor
    from coordinare.services.environment_attempt import AttemptBudget
    from tests.unit.test_353_discovery_repair import PATH, SOURCE
    from tests.unit.test_353_execution_source_pipeline import bundle_fixture

    bundle = bundle_fixture(tmp_path, {PATH: (SOURCE.encode(), '100644')})
    data = private_plan().model_dump(mode='json')
    data.update(execution_source_digest=bundle.identity.digest(), inputs={PATH: hashlib.sha256(SOURCE.encode()).hexdigest()})
    data['request']['commands'].append({**data['request']['commands'][0], 'id': 'second'})
    data['request']['citations']['second'] = [PATH]
    data['request']['questions']['second'] = 'Independent second observation'
    selected = ProbePlan.model_validate(data)
    monkeypatch.setattr(executor, '_docker_identity', AsyncMock(return_value=(selected.capabilities.image_ref, selected.capabilities.platform)))
    monkeypatch.setattr(executor, '_remove', AsyncMock(return_value=True))
    scratch_paths, payloads = [], []
    async def docker(args, **kwargs):
        if args[1] == 'create':
            mount = next(value for value in args if 'target=/scratch' in value)
            path = Path(mount.split('source=', 1)[1].split(',', 1)[0])
            assert path.is_dir() and list(path.iterdir()) == []
            (path/'previous-command').write_text('must never reach another command')
            scratch_paths.append(path)
        if args[1] == 'exec':
            payload = json.loads(kwargs['stdin'])
            payloads.append(payload)
            return 0, json.dumps([{'id': payload['commands'][0]['id'], 'status': 'completed', 'exit_code': 0,
                                   'output': 'platform observation', 'truncated': False}]).encode()
        return 0, b''
    monkeypatch.setattr(executor, '_docker', docker)
    deadline = time.monotonic()+10
    report = await executor.execute_environment_probe(selected, state={'probe_backend': 'docker'},
        source_bundle=bundle, budget=AttemptBudget(), deadline=deadline)
    assert report.cleanup_confirmed and len(report.results) == 2
    assert len(set(scratch_paths)) == 2 and all(not path.exists() for path in scratch_paths)
    assert all(payload['execution_source_digest'] == bundle.identity.digest() for payload in payloads)
    assert all(payload['runtime']['identity']['carrier_image'] == selected.capabilities.image_ref for payload in payloads)
    assert all(0 < payload['expires_at']-time.time() <= 10 for payload in payloads)
    assert all('runtime_artifacts' not in payload for payload in payloads)


def test_private_probe_cannot_omit_full_source_identity():
    raw = private_plan().model_dump(mode='json')
    raw['execution_source_digest'] = None
    with pytest.raises(ValueError, match='source identity'):
        ProbePlan.model_validate(raw)


def test_observation_templates_home_and_cwd_use_the_exported_private_scratch(tmp_path):
    import sys

    from coordinare_service_inference.environment.observation import execute

    scratch = tmp_path/'exported'
    scratch.mkdir()
    result = execute({'id': 'paths', 'argv': [sys.executable, '-c',
        'import os;print(os.getcwd());print(os.environ["HOME"]);print(os.environ["TMPDIR"])'],
        'cwd': '{scratch}', 'env': {}, 'timeout_seconds': 2}, 2,
        roots={'repo': str(tmp_path/'repo'), 'cache': str(tmp_path/'cache'), 'scratch': str(scratch)})
    assert result['status'] == 'completed' and result['exit_code'] == 0
    assert result['output'].splitlines() == [str(scratch)]*3


@pytest.mark.asyncio
async def test_a_full_batch_reserves_teardown_time_and_names_the_failed_cleanup(tmp_path, monkeypatch):
    import sys
    import time
    from types import SimpleNamespace

    from coordinare_service_inference.environment import runtime_observation as runner

    observed = {}

    class Lease:
        deadline = time.monotonic() + 600

        def __init__(self):
            self.activation_env = {'DOCKER_HOST': 'ssh://root@127.0.0.1:2222', 'COORDINARE_ENV_RUNTIME_BIN': '/private/job/bin'}

        def check_bounds(self):
            pass

        async def close(self, *, deadline):
            observed['close_deadline'] = deadline
            return SimpleNamespace(confirmed=True, qemu_reaped=True, keys_absent=True, listeners_absent=True,
                                   artifacts_unchanged=True, clean_shutdown=False, bounds_respected=True)

    async def open_runtime(spec, **kwargs):
        observed.update(kwargs)
        return Lease()

    monkeypatch.setattr(runner, 'open_runtime', open_runtime)
    data = {'commands': [{'id': 'probe', 'argv': [sys.executable, '-c', 'print(1)'], 'cwd': str(tmp_path), 'env': {},
                          'timeout_seconds': 2}], 'seconds': 1800, 'expires_at': time.time() + 1800,
            'runtime': policy_data(), 'execution_source_digest': 'a' * 64}
    started = time.monotonic()
    with pytest.raises(runner.PrivateTeardownError, match='clean_shutdown'):
        await runner.observe_private(data, repo=tmp_path / 'repo', cache=tmp_path / 'cache', scratch=tmp_path)
    assert 179 <= observed['deadline'] - started <= 181
    assert observed['close_deadline'] == observed['deadline']
