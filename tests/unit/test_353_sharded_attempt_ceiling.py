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
    assert (MAX_ATTEMPT_SECONDS, MAX_SHARDED_ATTEMPT_SECONDS) == (3600, 14400)
    assert EnvCacheConfig(manifest_shard_context_tokens=262144).manifest_attempt_timeout_seconds == 14400
    with pytest.raises(ValueError, match='one hour'):
        EnvCacheConfig(manifest_attempt_timeout_seconds=7200)


def test_limits_allow_longer_attempts_only_when_sharded():
    DiscoveryLimits(attempt_timeout_seconds=14400, shard_context_tokens=262144)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=7200)
    with pytest.raises(ValueError):
        DiscoveryLimits(attempt_timeout_seconds=14401, shard_context_tokens=262144)
    AttemptBudget(timeout_seconds=14400)


def test_begin_attempt_caps_by_mode(tmp_path):
    from coordinare.services.environment_repair import begin_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    def deadline(limits):
        cache = cache_fixture(tmp_path / str(limits.shard_context_tokens))
        cache.bootstrap_commit_sha = 'a' * 40
        budget = begin_attempt(cache, limits, outer_seconds=0, now=1000.0)
        return round(budget.timeout_seconds)

    assert deadline(DiscoveryLimits(attempt_timeout_seconds=3600)) == 3600
    assert deadline(DiscoveryLimits(attempt_timeout_seconds=14400, shard_context_tokens=262144)) == 14400


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
