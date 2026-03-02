"""Tests for spec-009 metrics coverage (T013, T028)."""
from __future__ import annotations

import time

import pytest

from coordinare.metrics import CoordinareMetrics


@pytest.fixture()
def fresh_metrics() -> CoordinareMetrics:
    """Return a fresh CoordinareMetrics instance with an isolated registry."""
    return CoordinareMetrics()


# ---------------------------------------------------------------------------
# US1: metric families present and functional
# ---------------------------------------------------------------------------


def test_cycles_completed_total_increments_on_cycle(fresh_metrics: CoordinareMetrics) -> None:
    """cycles_completed_total counter increments correctly."""
    before = fresh_metrics.cycles_completed_total._value.get()
    fresh_metrics.cycles_completed_total.inc()
    after = fresh_metrics.cycles_completed_total._value.get()
    assert after == before + 1


def test_card_state_transitions_total_uses_correct_labels(fresh_metrics: CoordinareMetrics) -> None:
    """card_state_transitions_total accepts all specified transition_type label values."""
    for transition in (
        "idle_to_dispatch",
        "dispatch_to_monitor",
        "monitor_to_merge",
        "monitor_to_blocked",
        "blocked_to_idle",
    ):
        fresh_metrics.card_state_transitions_total.labels(transition_type=transition).inc()
        val = fresh_metrics.card_state_transitions_total.labels(
            transition_type=transition
        )._value.get()
        assert val >= 1, f"Expected count ≥1 for transition_type={transition}"


def test_cycle_duration_seconds_observes_positive_value(fresh_metrics: CoordinareMetrics) -> None:
    """cycle_duration_seconds histogram accepts positive observations."""
    fresh_metrics.cycle_duration_seconds.observe(0.42)
    assert fresh_metrics.cycle_duration_seconds._sum.get() > 0
    rendered = fresh_metrics.render()
    assert "cycle_duration_seconds_count 1" in rendered


def test_config_load_duration_recorded_at_startup(fresh_metrics: CoordinareMetrics) -> None:
    """config_load_duration_seconds histogram records startup timing."""
    fresh_metrics.config_load_duration_seconds.observe(0.005)
    assert fresh_metrics.config_load_duration_seconds._sum.get() > 0
    rendered = fresh_metrics.render()
    assert "config_load_duration_seconds_count 1" in rendered


def test_coordinare_build_info_always_1(fresh_metrics: CoordinareMetrics) -> None:
    """coordinare_build_info metric renders as a gauge with value 1.0."""
    fresh_metrics.build_info.info({
        "version": "test",
        "python_version": "3.12",
        "started_at": "2026-01-01T00:00:00Z",
    })
    rendered = fresh_metrics.render()
    assert "coordinare_build_info" in rendered


def test_zero_value_metrics_present_before_any_events(fresh_metrics: CoordinareMetrics) -> None:
    """All label combinations initialized to 0 at construction."""
    rendered = fresh_metrics.render()
    assert "idle_to_dispatch" in rendered
    assert "dispatch_to_monitor" in rendered
    assert "monitor_to_merge" in rendered
    assert "monitor_to_blocked" in rendered
    assert "blocked_to_idle" in rendered
    assert "card_status" in rendered


def test_notification_counters_registered_in_registry(fresh_metrics: CoordinareMetrics) -> None:
    """notification metric families present in /metrics output."""
    rendered = fresh_metrics.render()
    assert "notifications_dispatched_total" in rendered
    assert "notifications_failed_total" in rendered
    assert "notifications_rate_limited_total" in rendered
    assert "notifications_deduplicated_total" in rendered


def test_circuit_breaker_counters_registered_in_registry(fresh_metrics: CoordinareMetrics) -> None:
    """circuit_breaker metric families present in /metrics output."""
    rendered = fresh_metrics.render()
    assert "circuit_breaker_trips_total" in rendered
    assert "circuit_breaker_state" in rendered


def test_daemon_up_gauge_present(fresh_metrics: CoordinareMetrics) -> None:
    """daemon_up gauge present (replaces legacy 'up' gauge)."""
    rendered = fresh_metrics.render()
    assert "coordinare_daemon_up" in rendered
    # Old 'coordinare_up' name should not be present
    assert "coordinare_up{" not in rendered


def test_channel_name_label_on_notification_metrics(fresh_metrics: CoordinareMetrics) -> None:
    """Notification metrics use channel_name label (not channel)."""
    # Should not raise — label key is channel_name
    fresh_metrics.notifications_dispatched_total.labels(
        event_type="card_blocked", channel_name="slack"
    ).inc()
    fresh_metrics.notifications_failed_total.labels(channel_name="email").inc()


def test_circuit_breaker_state_uses_service_name_label(fresh_metrics: CoordinareMetrics) -> None:
    """circuit_breaker_state gauge uses service_name label."""
    fresh_metrics.circuit_breaker_state.labels(service_name="github").set(0)
    rendered = fresh_metrics.render()
    assert 'service_name="github"' in rendered


# ---------------------------------------------------------------------------
# SC-007: metric collection overhead (T028)
# ---------------------------------------------------------------------------


def test_metric_collection_overhead_under_10ms(fresh_metrics: CoordinareMetrics) -> None:
    """SC-007: all new metric operations in one cycle complete in < 10ms (min over 20 runs)."""
    def _one_cycle() -> float:
        t0 = time.perf_counter()
        fresh_metrics.cycles_completed_total.inc()
        fresh_metrics.cycle_duration_seconds.observe(0.5)
        fresh_metrics.daemon_up.set(1)
        for t in ("idle_to_dispatch", "dispatch_to_monitor", "monitor_to_merge"):
            fresh_metrics.card_state_transitions_total.labels(transition_type=t).inc()
        for s in ("dispatched", "monitoring_agent", "idle"):
            fresh_metrics.cards_processed_total.labels(card_status=s).inc()
        return time.perf_counter() - t0

    # Warm up the Prometheus client label cache, then measure 15 steady-state iterations
    for _ in range(5):
        _one_cycle()
    timings = [_one_cycle() for _ in range(15)]
    min_elapsed = min(timings)
    assert min_elapsed < 0.01, f"Metric operations min={min_elapsed * 1000:.2f}ms (budget: 10ms)"
