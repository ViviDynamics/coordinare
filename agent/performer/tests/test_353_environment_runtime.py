"""A runtime must outlive initial verification and stay active for ordinary tools."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt, RuntimeSpec
from coordinare_service_inference.environment.runtime_supervisor import RuntimeCleanupReport
from performer import environment_runtime
from performer.environment_contract import close_environment, prepare_environment
from performer.models import Stand

from .source_contract_fixture import current_review_fixture
from .test_351_environment_contract import contract_score


def private_score(tmp_path):
    score = contract_score(tmp_path)
    score.role = 'implementing'
    spec = RuntimeSpec.model_validate({'schema_version': 1, 'endpoints': [], 'policy': {
        'schema_version': 1, 'kind': 'private_service_guest', 'control_port': 2222, 'max_endpoints': 16,
        'identity': {'schema_version': 1, 'carrier_image': 'example/carrier@sha256:'+'a'*64,
                     'carrier_platform': 'linux/aarch64', 'guest_platform': 'linux/x86_64',
                     'guest_base_sha256': 'b'*64, 'emulator_sha256': 'c'*64,
                     'firmware_sha256': 'd'*64, 'configuration_sha256': 'e'*64},
        'resources': {'cpus': 2, 'memory_mib': 4096, 'cache_bytes': 8*1024**3,
                      'scratch_bytes': 16*1024**3, 'reserve_bytes': 16*1024**3, 'max_seconds': 1800}}})
    manifest = EnvironmentManifest.model_validate({**score.environment_manifest, 'schema_version': 2,
        'policy_version': '353-v2', 'runtime': spec.model_dump(), 'install': [], 'services': [],
        'activation': {'path': [], 'env': {}}, 'checks': [{'id': 'check', 'argv': [sys.executable, '-c',
        'import os;assert os.environ["DOCKER_HOST"] == "ssh://private-fixture"']}]})
    score.environment_manifest = manifest.model_dump()
    score.environment_manifest_digest = manifest.digest()
    score.environment_assessment = current_review_fixture(manifest, {**score.environment_assessment, 'manifest_digest': manifest.digest()})
    source = ExecutionSourceIdentity.model_validate(score.environment_execution_source)
    score.environment_runtime_artifacts = RuntimeArtifactReceipt(schema_version=1, manifest_digest=manifest.digest(),
        source_digest=source.digest(), runtime_digest=spec.digest(), sealed=True,
        artifacts=[{'path': 'installed.qcow2', 'sha256': 'f'*64, 'bytes': 1234}]).model_dump()
    return score, manifest, source


@pytest.mark.asyncio
async def test_regular_shell_keeps_the_same_runtime_until_job_cleanup(tmp_path, monkeypatch):
    from performer.backends._env_policy import build_subprocess_env

    score, manifest, source = private_score(tmp_path)
    monkeypatch.setenv('DOCKER_HOST', 'caller-original')
    state = {'closed': False}
    async def close(*, deadline):
        state['closed'] = True
        return RuntimeCleanupReport(True, True, True, True, True)
    lease = SimpleNamespace(spec=manifest.runtime, manifest_digest=manifest.digest(), source_digest=source.digest(),
        install=False, deadline=time.monotonic()+10, scratch=tmp_path, close=close, check_bounds=lambda: None,
        activation_env={'DOCKER_HOST': 'ssh://private-fixture', 'DOCKER_CONFIG': '/owned/config',
                        'DOCKER_CONTEXT': '', 'PATH': os.environ['PATH']})
    async def open_runtime(*args, **kwargs):
        return lease
    monkeypatch.setattr(environment_runtime, 'open_runtime', open_runtime)
    stand = Stand(path=tmp_path, branch='main')
    await prepare_environment(score, stand)
    try:
        assert not state['closed']
        environment = build_subprocess_env(cache_env=stand.cache_env)
        command = subprocess.run(['/bin/bash', '-c', 'test "$DOCKER_HOST" = ssh://private-fixture'],
                                 env=environment, capture_output=True, text=True, timeout=5)
        assert command.returncode == 0, command.stderr
        assert os.environ['DOCKER_HOST'] == 'caller-original'
    finally:
        await close_environment(stand)
    assert state['closed']


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['missing', 'source', 'runtime'])
async def test_bad_receipt_refuses_before_opening_runtime_or_running_commands(tmp_path, monkeypatch, problem):
    score, _, _ = private_score(tmp_path)
    if problem == 'missing':
        score.environment_runtime_artifacts = None
    else:
        score.environment_runtime_artifacts[problem+'_digest'] = '9'*64
    async def forbidden(*args, **kwargs):
        pytest.fail('invalid receipt started a runtime')
    monkeypatch.setattr(environment_runtime, 'open_runtime', forbidden)
    with pytest.raises(ValueError, match=r'receipt|identity'):
        await prepare_environment(score, Stand(path=tmp_path, branch='main'))


def test_harness_prioritizes_owned_ssh_wrapper_without_changing_other_image_tools(tmp_path):
    from performer.backends._env_policy import build_subprocess_env

    image = tmp_path/'image'
    runtime = tmp_path/'runtime'
    cache = tmp_path/'cache'
    for path in (image, runtime, cache):
        path.mkdir()
    for directory, name, output in ((image, 'node', 'image-node'), (cache, 'node', 'cache-node'),
                                     (image, 'ssh', 'image-ssh'), (runtime, 'ssh', 'owned-ssh')):
        binary = directory/name
        binary.write_text('#!/bin/sh\necho '+output+'\n')
        binary.chmod(0o700)
    env = build_subprocess_env(base_env={'PATH': str(image)}, cache_env={
        'PATH': str(runtime)+':'+str(cache), 'COORDINARE_ENV_RUNTIME_BIN': str(runtime)})
    assert subprocess.check_output(['/bin/sh', '-c', 'exec ssh'], env=env, text=True, timeout=5).strip() == 'owned-ssh'
    assert subprocess.check_output(['/bin/sh', '-c', 'exec node'], env=env, text=True, timeout=5).strip() == 'image-node'
