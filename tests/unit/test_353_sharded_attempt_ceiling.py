"""Sharded discovery may run up to four hours; every other attempt keeps its one-hour ceiling."""
from __future__ import annotations

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import (
    MAX_ATTEMPT_SECONDS,
    MAX_SHARDED_ATTEMPT_SECONDS,
    AttemptBudget,
)
from coordinare.services.environment_discovery import DiscoveryLimits


def test_the_sharded_ceiling_is_four_hours_and_the_default_one_hour_stays():
    assert (MAX_ATTEMPT_SECONDS, MAX_SHARDED_ATTEMPT_SECONDS) == (3600, 21600)
    assert EnvCacheConfig(manifest_shard_context_tokens=262144).manifest_attempt_timeout_seconds == 21600
    with pytest.raises(ValueError, match='one hour'):
        EnvCacheConfig(manifest_attempt_timeout_seconds=7200)


def test_limits_allow_longer_attempts_only_when_sharded():
    DiscoveryLimits(attempt_timeout_seconds=21600, shard_context_tokens=262144)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=7200)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=21601, shard_context_tokens=262144)
    AttemptBudget(timeout_seconds=21600)


def test_begin_attempt_caps_by_mode(tmp_path):
    from coordinare.services.environment_repair import begin_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    def deadline(limits):
        cache = cache_fixture(tmp_path / str(limits.shard_context_tokens))
        cache.bootstrap_commit_sha = 'a' * 40
        budget = begin_attempt(cache, limits, outer_seconds=0, now=1000.0)
        return round(budget.timeout_seconds)

    assert deadline(DiscoveryLimits(attempt_timeout_seconds=3600)) == 3600
    assert deadline(DiscoveryLimits(attempt_timeout_seconds=21600, shard_context_tokens=262144)) == 21600


def test_a_phase_over_one_hour_requires_sharded_discovery():
    import pytest
    from pydantic import ValidationError

    from coordinare.config import EnvCacheConfig

    assert EnvCacheConfig(manifest_phase_timeout_seconds=7200,
                          manifest_shard_context_tokens=262144).manifest_phase_timeout_seconds == 7200
    with pytest.raises(ValidationError):
        EnvCacheConfig(manifest_phase_timeout_seconds=7200)
    with pytest.raises(ValidationError):
        EnvCacheConfig(manifest_phase_timeout_seconds=5 * 3600, manifest_shard_context_tokens=262144)


def test_each_execution_repair_gets_its_own_schema_correction_budget(tmp_path):
    """Maintainer ruling 2026-10-29: e2e-11's construction review used six of eight corrections, leaving its repair two."""
    from coordinare.services.environment_repair import (
        _publish,
        journal_for,
        prepare_repair,
        record_execution_failure,
    )
    from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started

    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    journal = journal_for(cache)
    journal.schema_repairs = 6
    _publish(cache, journal)
    prepare_repair(cache)
    assert journal_for(cache).schema_repairs == 0
