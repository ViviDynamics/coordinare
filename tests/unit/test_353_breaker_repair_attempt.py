"""A repair the attempt journal granted continues one attempt; it does not spend the bootstrap circuit breaker.

Dettmore e2e-70: every failed execution spent the 088 breaker (three per spec), so it tripped after the second
repair while the attempt still allowed two more and had two hours left. The production daemon then dispatches
nothing, and the pending repair never runs.
"""
from __future__ import annotations

from coordinare.services.environment_repair import record_execution_failure
from tests.unit.test_353_bootstrap_pipeline import setup as service_setup
from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started


def _service_with(tmp_path, *, cleanup_confirmed: bool):
    (tmp_path / "service").mkdir()
    service, _, state, _ = service_setup(tmp_path / "service")
    cache = cache_fixture(tmp_path)
    started(cache)
    record_execution_failure(cache, failed_report(cache), cleanup_confirmed=cleanup_confirmed, now=101)
    state["env_cache"] = {"suite": cache}
    return service, cache, state


def test_failures_the_journal_repairs_never_trip_the_breaker(tmp_path):
    service, cache, state = _service_with(tmp_path, cleanup_confirmed=True)
    assert cache.environment_attempt["status"] == "repair_pending"
    for _ in range(4):
        service.on_bootstrap_complete("suite", False, state, error="bootstrap failed")
    assert cache.bootstrap_attempts == 0
    assert not cache.bootstrap_exhausted


def test_an_attempt_that_ends_held_spends_the_breaker(tmp_path):
    service, cache, state = _service_with(tmp_path, cleanup_confirmed=False)
    assert cache.environment_attempt["status"] == "held"
    service.on_bootstrap_complete("suite", False, state, error="bootstrap failed")
    assert cache.bootstrap_attempts == 1
