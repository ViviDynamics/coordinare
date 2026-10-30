"""Private runtime artifacts and policy changes cannot reuse native readiness."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from coordinare_service_inference.environment.models import ExecutionCapabilities
from coordinare_service_inference.environment.runtime import (
    RuntimeArtifactReceipt,
    RuntimeEndpoint,
    RuntimePolicy,
    RuntimeSpec,
)
from pydantic import ValidationError

IMAGE = 'registry.example/performer@sha256:' + 'a' * 64


def policy_data():
    return {
        'schema_version': 1, 'kind': 'private_service_guest',
        'identity': {
            'schema_version': 1, 'carrier_image': IMAGE, 'carrier_platform': 'linux/aarch64',
            'guest_base_sha256': 'b' * 64, 'guest_platform': 'linux/x86_64',
            'emulator_sha256': 'c' * 64, 'firmware_sha256': 'd' * 64,
            'configuration_sha256': 'e' * 64,
        },
        'resources': {'cpus': 2, 'memory_mib': 4096, 'cache_bytes': 8 * 1024**3,
                      'scratch_bytes': 16 * 1024**3, 'reserve_bytes': 16 * 1024**3,
                      'max_seconds': 1800},
        'control_port': 2222, 'max_endpoints': 16,
    }


def native_data():
    return {'schema_version': 1, 'image_ref': IMAGE, 'platform': 'linux/aarch64',
            'cache_mount_path': '/devenv/sym', 'install_network': 'enabled', 'verify_network': 'disabled'}


def test_legacy_native_capability_digest_remains_identical():
    raw = native_data()
    native = ExecutionCapabilities.model_validate(raw)
    assert native.model_dump() == raw
    assert native.digest() == hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize(('section', 'field', 'value'), [
    ('identity', 'carrier_image', 'registry.example/performer:latest'),
    ('identity', 'guest_base_sha256', ''),
    ('identity', 'emulator_sha256', 'qemu:latest'),
    ('identity', 'schema_version', 2),
    ('identity', 'guest_platform', 'darwin/x86_64'),
    ('resources', 'cpus', 0),
    ('resources', 'memory_mib', 0),
    ('resources', 'cache_bytes', 0),
    ('resources', 'scratch_bytes', 0),
    ('resources', 'reserve_bytes', 0),
    ('resources', 'max_seconds', 3601),
])
def test_runtime_policy_rejects_mutable_or_unbounded_inputs(section, field, value):
    raw = policy_data()
    raw[section][field] = value
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate(raw)


def test_runtime_capability_binds_carrier_and_guest_separately():
    policy = RuntimePolicy.model_validate(policy_data())
    raw = {**native_data(), 'schema_version': 2, 'runtime': policy.model_dump()}
    capabilities = ExecutionCapabilities.model_validate(raw)
    assert capabilities.platform == 'linux/aarch64'
    assert capabilities.runtime.identity.guest_platform == 'linux/x86_64'
    assert ExecutionCapabilities.model_validate_json(capabilities.model_dump_json()) == capabilities
    for field, value in [('image_ref', 'other/image@sha256:' + 'f' * 64), ('platform', 'linux/x86_64')]:
        with pytest.raises(ValidationError, match='carrier'):
            ExecutionCapabilities.model_validate({**raw, field: value})
    with pytest.raises(ValidationError):
        ExecutionCapabilities.model_validate({**raw, 'schema_version': 1})
    with pytest.raises(ValidationError):
        ExecutionCapabilities.model_validate({k: v for k, v in raw.items() if k != 'runtime'})


@pytest.mark.parametrize(('section', 'field', 'value'), [
    ('identity', 'guest_base_sha256', 'f' * 64),
    ('identity', 'emulator_sha256', 'f' * 64),
    ('identity', 'firmware_sha256', 'f' * 64),
    ('identity', 'configuration_sha256', 'f' * 64),
    ('identity', 'guest_platform', 'linux/aarch64'),
    ('resources', 'cpus', 3),
    ('resources', 'memory_mib', 8192),
    ('resources', 'scratch_bytes', 32 * 1024**3),
])
def test_any_runtime_policy_change_changes_capability_identity(section, field, value):
    raw = {**native_data(), 'schema_version': 2, 'runtime': policy_data()}
    original = ExecutionCapabilities.model_validate(raw)
    raw['runtime'][section][field] = value
    changed = ExecutionCapabilities.model_validate(raw)
    assert changed.digest() != original.digest()


def test_endpoint_contract_rejects_unsafe_binds_and_control_collisions():
    endpoint = {'id': 'http', 'bind_address': '127.0.0.1', 'guest_port': 8080, 'outer_port': 8080}
    for changes in ({'bind_address': '0.0.0.0'}, {'outer_port': 0}, {'guest_port': 65536}):
        with pytest.raises(ValidationError):
            RuntimeEndpoint.model_validate({**endpoint, **changes})
    policy = RuntimePolicy.model_validate(policy_data())
    def compile_spec(endpoints):
        return RuntimeSpec.model_validate({'schema_version': 1, 'policy': policy.model_dump(), 'endpoints': endpoints})
    spec = compile_spec([endpoint])
    assert RuntimeSpec.model_validate_json(spec.model_dump_json()).digest() == spec.digest()
    assert compile_spec([{**endpoint, 'outer_port': 8081}]).digest() != spec.digest()
    for endpoints in ([endpoint, endpoint], [endpoint, {**endpoint, 'id': 'duplicate-port'}],
                      [{**endpoint, 'outer_port': 2222}], [{**endpoint, 'guest_port': 22}]):
        with pytest.raises(ValidationError):
            compile_spec(endpoints)


def test_artifact_receipt_requires_sealed_hashes_bound_to_manifest_source_and_runtime():
    spec = RuntimeSpec.model_validate({'schema_version': 1, 'policy': policy_data(), 'endpoints': []})
    raw = {'schema_version': 1, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
           'runtime_digest': spec.digest(), 'sealed': True,
           'artifacts': [{'path': 'installed.qcow2', 'sha256': '3' * 64, 'bytes': 1234}]}
    receipt = RuntimeArtifactReceipt.model_validate(raw)
    receipt.require_binding(manifest_digest='1' * 64, source_digest='2' * 64, runtime_digest=spec.digest())
    for field in ('manifest_digest', 'source_digest', 'runtime_digest'):
        kwargs = {key: raw[key] for key in ('manifest_digest', 'source_digest', 'runtime_digest')}
        kwargs[field] = '4' * 64
        with pytest.raises(ValueError, match='identity'):
            receipt.require_binding(**kwargs)
    for change in ({'sealed': False}, {'artifacts': []}, {'schema_version': 2}):
        with pytest.raises(ValidationError):
            RuntimeArtifactReceipt.model_validate({**raw, **change})
    for path in ('../escape', '/outside', 'nested/../../escape', 'disk\\escape'):
        bad = copy.deepcopy(raw)
        bad['artifacts'][0]['path'] = path
        with pytest.raises(ValidationError):
            RuntimeArtifactReceipt.model_validate(bad)


def test_runtime_receipt_survives_snapshot_but_does_not_grant_readiness(tmp_path):
    from types import SimpleNamespace

    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.state_store import EnvCacheStateSnapshot

    cache = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=tmp_path/'sym')
    runtime = RuntimeSpec.model_validate({'schema_version': 1, 'policy': policy_data(), 'endpoints': []})
    receipt = RuntimeArtifactReceipt.model_validate({
        'schema_version': 1, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
        'runtime_digest': runtime.digest(), 'sealed': True,
        'artifacts': [{'path': 'installed.qcow2', 'sha256': '3' * 64, 'bytes': 1234}],
    })
    cache.environment_runtime_artifacts = receipt.model_dump(mode='json')
    cache.environment_capabilities = ExecutionCapabilities.model_validate(
        {**native_data(), 'schema_version': 2, 'runtime': policy_data()},
    ).model_dump(mode='json')
    snapshot = _persist_env_cache({'sym': cache})['sym']
    snapshot = EnvCacheStateSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=cache.cache_dir)
    _rehydrate_env_cache({'env_cache': {'sym': restored}}, SimpleNamespace(env_cache={'sym': snapshot}))
    assert restored.environment_runtime_artifacts == cache.environment_runtime_artifacts
    assert restored.environment_capabilities == cache.environment_capabilities
    assert not restored.cache_dir_ready
    old = EnvCacheStateSnapshot.model_validate({'symphony_name': 'sym', 'sanitised_name': 'sym', 'cache_dir': str(cache.cache_dir)})
    assert old.environment_runtime_artifacts is None


@pytest.mark.parametrize('operation', ['clear', 'repair', 'restart'])
def test_replacement_generation_drops_old_runtime_receipts(tmp_path, operation):
    from coordinare.services.env_cache import _clear_reviewed_environment
    from coordinare.services.environment_repair import (
        prepare_repair,
        record_execution_failure,
        restart_environment_attempt,
    )
    from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started

    cache = cache_fixture(tmp_path)
    cache.environment_runtime_artifacts = {
        'schema_version': 1, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
        'runtime_digest': '3' * 64, 'sealed': True,
        'artifacts': [{'path': 'installed.qcow2', 'sha256': '4' * 64, 'bytes': 1234}],
    }
    if operation == 'clear':
        _clear_reviewed_environment(cache)
    else:
        started(cache)
        assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
        if operation == 'repair':
            prepare_repair(cache)
        else:
            restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_runtime_artifacts is None
    assert not cache.cache_dir_ready


@pytest.mark.parametrize('version', [True, 1.0, '1', 99])
def test_runtime_version_is_an_exact_supported_integer(version):
    raw = policy_data()
    raw['schema_version'] = version
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate(raw)


def test_repair_journal_rejects_changed_runtime_without_resetting_original_budget(tmp_path):
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_repair import RepairJournal, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture, started

    cache = cache_fixture(tmp_path)
    cache.environment_capabilities = ExecutionCapabilities.model_validate(
        {**native_data(), 'schema_version': 2, 'runtime': policy_data()},
    ).model_dump(mode='json')
    budget = started(cache, now=100, repairs=1)
    journal = RepairJournal.model_validate_json(json.dumps(cache.environment_attempt))
    assert journal.target.digest() == ExecutionCapabilities.model_validate(cache.environment_capabilities).digest()
    assert journal.expires_at == 700
    assert journal.execution_repairs == 1
    cache.environment_capabilities['runtime']['identity']['guest_base_sha256'] = 'f' * 64
    with pytest.raises(EnvironmentDiscoveryError, match='runtime identity changed'):
        save_attempt(cache, budget, executing=True)
    assert cache.environment_attempt['expires_at'] == 700
    assert cache.environment_attempt['execution_repairs'] == 1


def test_runtime_bundle_hashes_real_files_and_detects_changes(tmp_path):
    from coordinare_service_inference.environment.runtime_bundle import inspect_bundle

    paths = {}
    for key in ('guest_image', 'emulator', 'firmware'):
        file = tmp_path/key
        file.write_bytes((key + '-real-content').encode())
        paths[key] = str(file)
    raw = {'schema_version': 1, 'guest_platform': 'linux/x86_64', **paths}
    metadata = tmp_path/'bundle.json'
    metadata.write_text(json.dumps(raw))
    first = inspect_bundle(metadata)
    assert first['guest_base_sha256'] == hashlib.sha256(Path(paths['guest_image']).read_bytes()).hexdigest()
    Path(paths['guest_image']).write_bytes(b'changed-artifact')
    assert inspect_bundle(metadata)['guest_base_sha256'] != first['guest_base_sha256']
    metadata.write_text(json.dumps({**raw, 'guest_image': str(tmp_path/'missing')}))
    with pytest.raises(OSError):
        inspect_bundle(metadata)


@pytest.mark.asyncio
async def test_runtime_resolution_requires_actual_bundle_identity(monkeypatch):
    from types import SimpleNamespace

    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services import environment_capabilities as resolver

    policy = RuntimePolicy.model_validate(policy_data())
    actual = policy.identity.model_dump(exclude={'schema_version', 'carrier_image', 'carrier_platform'})
    async def image_identity(image):
        return IMAGE, 'linux/aarch64'
    async def bundle_identity(image):
        assert image == IMAGE
        return actual
    monkeypatch.setattr(resolver, '_docker_identity', image_identity)
    monkeypatch.setattr(resolver, '_inspect_runtime_bundle', bundle_identity)
    cache = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir='/tmp/sym')
    global_config = SimpleNamespace(agent_transport='docker', performer_image=IMAGE,
                                   env_cache=SimpleNamespace(private_service_runtime=policy))
    state = {'config': global_config, 'env_cache': {'sym': cache}}
    resolved = await resolver.resolve_execution_capabilities(state, 'sym')
    assert resolved.schema_version == 2
    assert resolved.runtime == policy
    actual['guest_base_sha256'] = 'f' * 64
    with pytest.raises(resolver.EnvironmentCapabilityError, match='runtime bundle identity'):
        await resolver.resolve_execution_capabilities(state, 'sym')
    global_config.agent_transport = 'kubernetes'
    with pytest.raises(resolver.EnvironmentCapabilityError, match=r'not supported.*Kubernetes'):
        await resolver.resolve_execution_capabilities(state, 'sym')


@pytest.mark.asyncio
async def test_cancelled_bundle_inspection_removes_its_daemon_side_container(monkeypatch):
    import asyncio

    from coordinare.services import environment_capabilities as resolver

    started = asyncio.Event()
    state = {'container_alive': False}
    class Process:
        returncode = None
        def __init__(self, kind):
            self.kind = kind
        async def communicate(self):
            if self.kind == 'run':
                state['container_alive'] = True
                started.set()
                await asyncio.Event().wait()
            if self.kind == 'rm':
                state['container_alive'] = False
                self.returncode = 0
                return b'', b''
            self.returncode = 1
            return b'', b'Error: No such container'
        def kill(self):
            self.returncode = -9  # Killing the client leaves the daemon resource alive.
        async def wait(self):
            return self.returncode
    async def spawn(*args, **kwargs):
        return Process('run' if args[1] == 'run' else 'rm' if args[1] == 'rm' else 'inspect')
    monkeypatch.setattr(resolver.asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(resolver._inspect_runtime_bundle(IMAGE))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state['container_alive'] is False


def test_optional_runtime_is_explicitly_parsed_without_changing_native_default():
    from coordinare.config import EnvCacheConfig

    assert EnvCacheConfig().private_service_runtime is None
    configured = EnvCacheConfig.model_validate({'private_service_runtime': policy_data()})
    assert configured.private_service_runtime == RuntimePolicy.model_validate(policy_data())
    bad = policy_data()
    bad['identity']['guest_base_sha256'] = 'main'
    with pytest.raises(ValidationError):
        EnvCacheConfig.model_validate({'private_service_runtime': bad})
