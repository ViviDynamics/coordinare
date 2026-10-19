"""Synthetic private-runtime contracts for daemon transport tests only."""
from __future__ import annotations

from agent.performer.tests.source_contract_fixture import current_review_fixture
from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
)
from coordinare_service_inference.environment.runtime import RuntimePolicy, RuntimeSpec

from coordinare.models.env_cache import EnvCacheState
from tests.unit.env_manifest_fixture import fixture_execution_source, reviewed_metadata
from tests.unit.test_353_runtime_identity import native_data, policy_data


def runtime_cache(tmp_path):
    policy = RuntimePolicy.model_validate(policy_data())
    capabilities = ExecutionCapabilities.model_validate({**native_data(), 'schema_version': 2, 'runtime': policy})
    metadata = reviewed_metadata(capabilities=capabilities, install=[{'id': 'install', 'argv': ['true']}], empty_reason='Synthetic protocol transport fixture')
    manifest = EnvironmentManifest.model_validate({**metadata['environment_manifest'], 'schema_version': 2,
        'policy_version': '353-v2', 'runtime': RuntimeSpec(schema_version=1, policy=policy, endpoints=[]).model_dump()})
    assessment = current_review_fixture(manifest, {**metadata['environment_assessment'], 'manifest_digest': manifest.digest()})
    cache = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=tmp_path)
    cache.environment_manifest = manifest.model_dump()
    cache.environment_assessment = assessment
    cache.environment_capabilities = capabilities.model_dump()
    cache.environment_execution_source = fixture_execution_source().model_dump()
    cache.bootstrap_commit_sha = manifest.source_commit
    return cache


def runtime_report(cache, *, phase='consumer'):
    manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    source = fixture_execution_source(manifest.source_commit)
    receipt = {'schema_version': 1, 'manifest_digest': manifest.digest(), 'source_digest': source.digest(),
               'runtime_digest': manifest.runtime.digest(), 'sealed': True,
               'artifacts': [{'path': 'installed.qcow2', 'sha256': 'f'*64, 'bytes': 100}]}
    return {'platform': manifest.runtime.policy.identity.carrier_platform,
            'manifest_digest': manifest.digest(), 'execution_source_digest': source.digest(), 'scope': '',
            'runtime_digest': manifest.runtime.digest(), 'runtime_phase': phase,
            'runtime_artifacts': receipt, 'runtime_cleanup': True,
            'results': [{'id': command.id, 'status': 'pass', 'required': True, 'exit_code': 0,
                         'manifest_digest': manifest.digest()} for command in [*manifest.install, *manifest.checks]]}
