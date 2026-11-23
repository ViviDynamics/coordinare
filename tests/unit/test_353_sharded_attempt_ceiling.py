"""Sharded discovery may run up to eight hours, its defaults keep six (one phase up to four); every other attempt keeps
its one-hour ceiling."""
from __future__ import annotations

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import (
    MAX_ATTEMPT_SECONDS,
    MAX_SHARDED_ATTEMPT_SECONDS,
    AttemptBudget,
)
from coordinare.services.environment_discovery import DiscoveryLimits


def test_the_sharded_ceiling_is_eight_hours_its_default_six_and_the_default_one_hour_stays():
    assert (MAX_ATTEMPT_SECONDS, MAX_SHARDED_ATTEMPT_SECONDS) == (3600, 28800)
    assert EnvCacheConfig(manifest_shard_context_tokens=262144).manifest_attempt_timeout_seconds == 21600
    assert EnvCacheConfig(manifest_shard_context_tokens=262144,
                          manifest_attempt_timeout_seconds=28800).manifest_attempt_timeout_seconds == 28800
    with pytest.raises(ValueError):
        EnvCacheConfig(manifest_shard_context_tokens=262144, manifest_attempt_timeout_seconds=28801)
    with pytest.raises(ValueError, match='one hour'):
        EnvCacheConfig(manifest_attempt_timeout_seconds=7200)


def test_limits_allow_longer_attempts_only_when_sharded():
    DiscoveryLimits(attempt_timeout_seconds=21600, shard_context_tokens=262144)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=7200)
    DiscoveryLimits(attempt_timeout_seconds=28800, shard_context_tokens=262144)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=28801, shard_context_tokens=262144)
    DiscoveryLimits(attempt_timeout_seconds=21600, phase_timeout_seconds=4 * 3600, shard_context_tokens=262144)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=21600, phase_timeout_seconds=5 * 3600, shard_context_tokens=262144)
    AttemptBudget(timeout_seconds=28800)


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
    assert deadline(DiscoveryLimits(attempt_timeout_seconds=28800, shard_context_tokens=262145)) == 28800


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


def test_each_execution_repair_gets_its_own_coverage_revisions(tmp_path):
    """Maintainer ruling 2026-11-23: nare-native-031 held on attempt_coverage_limit in its sixth generation, its first
    review having used four of the attempt's eight coverage revisions."""
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
    journal.coverage_revisions = journal.max_coverage_revisions
    _publish(cache, journal)
    prepare_repair(cache)
    assert journal_for(cache).coverage_revisions == 0
