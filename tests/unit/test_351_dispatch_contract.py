"""Dispatch preserves reviewed contracts and recovers from runtime identity drift."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coordinare.graph.nodes import dispatch_performer as node
from coordinare.models.env_cache import EnvCacheState
from tests.unit.env_manifest_fixture import reviewed_cache


@pytest.mark.asyncio
async def test_bootstrap_dispatch_carries_contract_without_consumer_provenance(tmp_path, monkeypatch):
    cache = reviewed_cache(EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=tmp_path))
    cache.environment_verification = None  # Bootstrap has a target, not clean execution evidence yet.
    cache.environment_attempt = {'expires_at': 1234567890.0}
    state = {'current_symphony': 'sym', 'env_cache': {'sym': cache}}
    context = {}

    async def mount(*args):
        context['env_cache_path'] = '/devenv/sym'
        return []

    monkeypatch.setattr(node, '_resolve_env_volumes', mount)
    monkeypatch.setattr(node, '_inject_test_env_vars', AsyncMock())
    _, terminal = await node._prepare_env_dispatch(state, SimpleNamespace(), 'card', 'env_bootstrap', context, Mock())
    assert terminal is None
    assert context['environment_manifest'] == cache.environment_manifest
    assert context['environment_assessment'] == cache.environment_assessment
    assert context['environment_manifest_digest'] == cache.environment_assessment['manifest_digest']
    assert 'env_provenance' not in context
    assert context['environment_verification_image'] == cache.environment_capabilities['image_ref']
    assert context['environment_verification_platform'] == cache.environment_capabilities['platform']
    assert context['environment_attempt_expires_at'] == 1234567890.0
    assert context['environment_runtime_artifacts'] is None


@pytest.mark.asyncio
async def test_identity_refusal_invalidates_cache_and_holds_for_bounded_rebuild():
    cache_service = SimpleNamespace(mark_runtime_health_failed=Mock())
    state = {'current_symphony': 'sym', 'env_cache_service': cache_service}
    release = Mock()
    result = {'status': 'error', 'reason_code': 'environment_identity_mismatch', 'reason': 'platform differs'}
    await node._dispatch_result_error(state, result, {'card_id': 'card', 'performer_stage': 'implementer'}, release)
    cache_service.mark_runtime_health_failed.assert_called_once_with('sym', state)
    release.assert_called_once()
    assert state['phase'] == 'idle'
    assert not state.get('system_error_count')


@pytest.mark.asyncio
async def test_current_consumer_image_drift_already_schedules_rebuild(tmp_path, monkeypatch):
    from coordinare.services.http_performer_service import HTTPPerformerService

    cache = reviewed_cache(EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=tmp_path))
    cache_service = SimpleNamespace(mark_runtime_health_failed=Mock())
    state = {'env_cache_service': cache_service}
    service = object.__new__(HTTPPerformerService)
    service._config = SimpleNamespace(mode='ephemeral', image='trusted/global@sha256:' + 'b' * 64)
    monkeypatch.setattr(node, '_performer_mounts', lambda _: ())
    monkeypatch.setattr(node, 'verify_env_cache_clean', AsyncMock(return_value=(True, 'saved Kubernetes evidence')))
    release = Mock()
    terminal = await node._env_readiness_gate(state, service, 'card', 'implementer', 'sym', cache, False, release)
    assert terminal is state
    cache_service.mark_runtime_health_failed.assert_called_once_with('sym', state)
    release.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_capability_error_does_not_request_identity_rebuild():
    cache_service = SimpleNamespace(mark_runtime_health_failed=Mock())
    state = {'current_symphony': 'sym', 'env_cache_service': cache_service}
    result = {'status': 'error', 'reason': 'consumer runtime platform differs; missing capability'}
    await node._dispatch_result_error(state, result, {'card_id': 'card', 'performer_stage': 'implementer'}, Mock())
    cache_service.mark_runtime_health_failed.assert_not_called()
    assert state['phase'] == 'system_error'
