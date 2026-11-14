"""A discovery its model provider stopped stays resumable from its journal, within the original deadline.

Dettmore e2e-57 held after 2.6 hours when the model gateway answered 502 for longer than the outage window: the hold
discarded accepted scope, review and two execution generations, although the journal kept every checkpoint.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from scripts.validate_environment_repositories import (
    MAX_PROVIDER_RESUMES,
    Profile,
    validate_repository,
)

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    dispatch_refusal,
    finish_attempt,
    journal_for,
    resumable_attempt,
    save_attempt,
)
from tests.unit.test_353_execution_repair import cache_fixture


def _stopped(cache, code):
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=time.time())
    budget.reserve_repair('schema')
    budget.error_code = code
    save_attempt(cache, budget)
    return budget


def test_a_provider_failure_leaves_the_attempt_resumable_with_its_spending_and_deadline(tmp_path):
    cache = cache_fixture(tmp_path)
    _stopped(cache, 'provider_request_failed')
    expires = cache.environment_attempt['expires_at']
    journal = journal_for(cache)
    assert (journal.status, journal.last_error) == ('discovering', 'provider_request_failed')
    assert resumable_attempt(cache) and dispatch_refusal(cache, cache.bootstrap_commit_sha) is None
    # The daemon's completion of the failed run keeps it resumable rather than holding it as refused discovery.
    finish_attempt(cache, False, None, cleanup_confirmed=False)
    assert resumable_attempt(cache)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=time.time())
    assert resumed.schema_repairs == 1 and journal_for(cache).last_error is None
    assert cache.environment_attempt['expires_at'] == expires


@pytest.mark.parametrize('code', ['attempt_schema_limit', 'model_reply_invalid', 'source_identity'])
def test_any_other_stop_still_holds(tmp_path, code):
    cache = cache_fixture(tmp_path)
    _stopped(cache, code)
    assert journal_for(cache).status == 'held' and not resumable_attempt(cache)
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=time.time())


def test_a_refused_discovery_without_a_provider_failure_still_holds(tmp_path):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=time.time())
    finish_attempt(cache, False, None, cleanup_confirmed=False)
    assert (journal_for(cache).status, journal_for(cache).last_error) == ('held', 'discovery_refused')


async def _validate(tmp_path, monkeypatch, outcomes):
    from coordinare.services.env_cache import EnvCacheService
    from tests.unit.env_manifest_fixture import reviewed_cache

    calls: list[float] = []

    async def verify():
        return None

    async def build(self, **kwargs):
        cache = kwargs['cache_state']
        budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
        calls.append(journal_for(cache).started_at)
        code = outcomes.pop(0)
        if code is None:
            reviewed_cache(cache)
            return
        budget.error_code = code
        save_attempt(cache, budget)
        raise EnvironmentDiscoveryError('environment model provider rejected or failed the request', code=code)

    monkeypatch.setattr(EnvCacheService, '_build_manifest_artifacts', build)
    monkeypatch.setattr(EnvCacheService, 'prepare_bootstrap_generation', lambda *args: None)
    snapshot = SimpleNamespace(name='suite', org='fixture', commit='a' * 40, verify=verify)
    result = await validate_repository(snapshot, image='trusted@sha256:' + 'a' * 64, output=tmp_path / 'profile',
                                       profile=Profile(model='fixture', base_url='http://localhost:1'),
                                       chat=lambda messages: None)
    return result, calls


@pytest.mark.asyncio
async def test_the_validator_resumes_the_same_attempt_after_a_provider_failure(tmp_path, monkeypatch):
    result, calls = await _validate(tmp_path, monkeypatch, ['provider_request_failed', None])
    # The second run continued the first attempt, not a new one.
    assert len(calls) == 2 and len(set(calls)) == 1
    assert result['status'] == 'accepted_not_executed' and result['outer_retry_loop_exercised'] is True
    assert [item['reason_code'] for item in result['provider_resumes']] == ['provider_request_failed']


@pytest.mark.asyncio
async def test_the_validator_resumes_a_bounded_number_of_times(tmp_path, monkeypatch):
    outcomes = ['provider_request_failed'] * (MAX_PROVIDER_RESUMES + 1)
    result, calls = await _validate(tmp_path, monkeypatch, outcomes)
    assert len(calls) == MAX_PROVIDER_RESUMES + 1
    assert (result['status'], result['reason_code']) == ('held', 'provider_request_failed')


@pytest.mark.asyncio
async def test_the_validator_never_resumes_any_other_stop(tmp_path, monkeypatch):
    result, calls = await _validate(tmp_path, monkeypatch, ['model_reply_invalid'])
    assert len(calls) == 1 and 'provider_resumes' not in result
    assert (result['status'], result['reason_code']) == ('held', 'model_reply_invalid')
