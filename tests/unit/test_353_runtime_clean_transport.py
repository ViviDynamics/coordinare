"""The daemon's actual clean-verifier command retains isolation and deadline controls."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coordinare_service_inference.environment.execution_source import (
    ExecutionSourceBundle,
    ExecutionSourceIdentity,
)
from coordinare_service_inference.environment.models import ExecutionCapabilities

from coordinare.services import env_cache as service
from coordinare.services import environment_runtime
from tests.unit.test_353_execution_source import bundle_file
from tests.unit.test_353_runtime_verifier_context import started_cache


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['executing', 'ready'])
async def test_runtime_clean_command_carries_sealed_receipt_limits_and_original_window(tmp_path, monkeypatch, status):
    cache, report = started_cache(tmp_path)
    now = 1001 if status == 'executing' else 4000
    cache.environment_attempt['status'] = status
    if status == 'ready':
        cache.environment_verification = {**report, 'image_ref': cache.environment_capabilities['image_ref']}
    context = environment_runtime.runtime_verifier_context(cache, now=now)
    cache.environment_verification = None
    cache.rendered_verify_sh = '# trusted synthetic verifier'
    source_root = tmp_path/'source'
    source_root.mkdir()
    path, identity = bundle_file(source_root, {'tools.plan': (b'baseline', '100644')}, commit=cache.bootstrap_commit_sha)
    source = ExecutionSourceIdentity.model_validate(identity)
    assert source.model_dump() == cache.environment_execution_source
    bundle = ExecutionSourceBundle(source, path, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    monkeypatch.setattr(service, 'ensure_execution_source', AsyncMock(return_value=bundle))
    monkeypatch.setattr(environment_runtime.time, 'time', lambda: now)
    capabilities = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    monkeypatch.setattr('coordinare.services.environment_capabilities.resolve_execution_capabilities', AsyncMock(return_value=capabilities))
    monkeypatch.setattr(service, '_remove_verify_container', AsyncMock(return_value=True))
    seen = {}
    class Process:
        returncode = 0
        async def communicate(self, input):
            seen['input'] = input
            return json.dumps(report).encode(), b''
    async def spawn(*args, **kwargs):
        seen['command'] = args
        return Process()
    monkeypatch.setattr(service.asyncio, 'create_subprocess_exec', spawn)
    passed, detail = await service._verify_cache_runtime_with_source({'env_cache': {'sym': cache}}, 'sym',
        SimpleNamespace(), (), environment_runtime.RuntimeVerifierSource(source_root, context))
    assert passed is True, detail
    command = seen['command']
    assert command[command.index('--network')+1] == 'none'
    assert command[command.index('--cap-drop')+1] == 'ALL'
    assert command[command.index('--security-opt')+1] == 'no-new-privileges'
    assert command[command.index('--cpus')+1] == '2'
    assert command[command.index('--memory')+1] == '4096m'
    assert command[command.index('--memory-swap')+1] == '4096m'
    assert any(item.endswith(':/tmp') and str(source_root) in item for item in command)
    assert b'COORDINARE_ENV_RUNTIME_ARTIFACTS=' in seen['input']
    assert str(context.expires_at).encode() in seen['input']
    assert cache.environment_verification['runtime_phase'] == 'consumer'
