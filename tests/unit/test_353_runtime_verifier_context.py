"""Clean verification transfers sealed receipts without renewing attempt budgets."""
from __future__ import annotations

import copy
import json

import pytest

from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt
from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report


def started_cache(tmp_path):
    cache = runtime_cache(tmp_path)
    report = runtime_report(cache)
    cache.environment_runtime_artifacts = report['runtime_artifacts']
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=120, now=1000)
    cache.environment_attempt['status'] = 'executing'
    return cache, report


def test_clean_runtime_context_preserves_original_expiry_and_receipt(tmp_path):
    from coordinare.services.environment_runtime import runtime_verifier_context

    cache, report = started_cache(tmp_path)
    before = copy.deepcopy(cache.environment_attempt)
    context = runtime_verifier_context(cache, now=1001)
    assert context.expires_at == 1120
    assert float(context.environment['COORDINARE_ENV_RUNTIME_EXPIRES_AT']) == 1120
    assert json.loads(context.environment['COORDINARE_ENV_RUNTIME_ARTIFACTS']) == report['runtime_artifacts']
    assert cache.environment_attempt == before


@pytest.mark.parametrize('problem', ['expired', 'held', 'repair_pending', 'missing_receipt', 'wrong_source', 'missing_attempt', 'capability_drift'])
def test_clean_runtime_context_holds_unknown_failed_and_expired_attempts(tmp_path, problem):
    from coordinare.services.environment_runtime import runtime_verifier_context

    cache, _ = started_cache(tmp_path)
    if problem in ('held', 'repair_pending'):
        cache.environment_attempt['status'] = problem
    elif problem == 'missing_receipt':
        cache.environment_runtime_artifacts = None
    elif problem == 'wrong_source':
        cache.environment_runtime_artifacts['source_digest'] = 'e'*64
    elif problem == 'missing_attempt':
        cache.environment_attempt = None
    elif problem == 'capability_drift':
        cache.environment_capabilities['runtime']['identity']['emulator_sha256'] = 'e'*64
    with pytest.raises(ValueError):
        runtime_verifier_context(cache, now=1200 if problem == 'expired' else 1001)


@pytest.mark.parametrize('verified', [False, True])
def test_later_consumer_health_job_requires_prior_clean_evidence(tmp_path, verified):
    from coordinare.services.environment_runtime import runtime_verifier_context

    cache, report = started_cache(tmp_path)
    cache.environment_attempt['status'] = 'ready'
    if verified:
        cache.environment_verification = {**report, 'image_ref': cache.environment_capabilities['image_ref']}
    before = copy.deepcopy(cache.environment_attempt)
    if verified:
        context = runtime_verifier_context(cache, now=4000)
        assert context.expires_at == 5800
        assert cache.environment_attempt == before
    else:
        with pytest.raises(ValueError):
            runtime_verifier_context(cache, now=4000)


@pytest.mark.asyncio
async def test_runtime_exports_are_present_even_without_credentials(tmp_path, monkeypatch):
    from coordinare.services import environment_runtime, environment_verification

    cache, report = started_cache(tmp_path)
    context = environment_runtime.runtime_verifier_context(cache, now=1001)
    script = await environment_verification.verification_environment({}, 'sym', cache, (), runtime_context=context)
    assert 'COORDINARE_ENV_RUNTIME_EXPIRES_AT=1120' in script
    assert 'COORDINARE_ENV_RUNTIME_ARTIFACTS=' in script
    assert report['runtime_artifacts']['runtime_digest'] in script


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['executing', 'ready'])
async def test_clean_gate_captures_context_before_discarding_prior_execution(tmp_path, monkeypatch, status):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare.services import env_cache as service
    from coordinare.services import environment_runtime

    cache, report = started_cache(tmp_path)
    cache.environment_attempt['status'] = status
    cache.rendered_verify_sh = '# synthetic trusted verifier'
    if status == 'ready':
        cache.environment_verification = {**report, 'image_ref': cache.environment_capabilities['image_ref']}
    monkeypatch.setattr(environment_runtime.time, 'time', lambda: 1001 if status == 'executing' else 4000)
    monkeypatch.setattr(service, '_verify_cache_refusal', lambda *args: None)
    monkeypatch.setattr(service, '_docker_daemon_reachable', AsyncMock(return_value=True))
    seen = []
    async def verify(*args, **kwargs):
        assert cache.environment_verification is None
        context = kwargs['runtime_context']
        assert context.expires_at == (1120 if status == 'executing' else 5800)
        seen.append(context)
        return False, 'transport boundary exercised'
    monkeypatch.setattr(service, '_verify_cache_runtime', verify)
    state = {'env_cache': {'sym': cache}}
    assert await service._verify_cache_clean(state, 'sym', SimpleNamespace(), performer_mounts=()) == (False, 'transport boundary exercised')
    assert len(seen) == 1
