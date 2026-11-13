"""Each execution repair brings one probe batch, and the journal must be able to persist it."""

from __future__ import annotations

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import environment_repair as repair
from coordinare.services.environment_attempt import MAX_PROBE_BATCHES, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt, save_attempt
from tests.unit.test_353_scope_contract import COMMIT


def _attempt(tmp_path):
    cache = EnvCacheState(
        symphony_name="sample", sanitised_name="sample", cache_dir=tmp_path / "sample", bootstrap_commit_sha=COMMIT,
    )
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    return cache, budget


@pytest.mark.parametrize("repairs", [1, 2])
def test_a_repair_probe_batch_survives_the_journal(tmp_path, repairs):
    cache, budget = _attempt(tmp_path)
    budget.execution_repairs = repairs
    budget.probe_batches = MAX_PROBE_BATCHES + repairs - 1
    budget.reserve_probe_batch()
    save_attempt(cache, budget)

    journal = repair.journal_for(cache)

    assert journal.probe_batches == MAX_PROBE_BATCHES + repairs


def test_the_batch_after_a_repairs_allowance_is_still_refused(tmp_path):
    _, budget = _attempt(tmp_path)
    budget.execution_repairs = 1
    budget.probe_batches = MAX_PROBE_BATCHES + 1
    with pytest.raises(EnvironmentDiscoveryError) as refused:
        budget.reserve_probe_batch()
    assert refused.value.code == "attempt_probe_limit"


def test_a_journal_claiming_more_batches_than_its_repairs_allow_is_invalid(tmp_path):
    cache, budget = _attempt(tmp_path)
    budget.execution_repairs = 1
    save_attempt(cache, budget)
    cache.environment_attempt["probe_batches"] = MAX_PROBE_BATCHES + 2
    with pytest.raises(EnvironmentDiscoveryError):
        repair.journal_for(cache)
