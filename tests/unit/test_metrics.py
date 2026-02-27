from __future__ import annotations

from coordinare.metrics import CoordinareMetrics


def test_notifications_dispatched_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.notifications_dispatched_total.labels(event_type="card_transition", channel="slack-ops").inc()
    assert metrics.notifications_dispatched_total.labels(
        event_type="card_transition", channel="slack-ops"
    )._value.get() == 1.0


def test_notifications_failed_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.notifications_failed_total.labels(channel="slack-ops").inc()
    assert metrics.notifications_failed_total.labels(channel="slack-ops")._value.get() == 1.0


def test_observe_error_increments_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.observe_error("graph_execution")

    assert metrics.errors_total.labels(category="graph_execution")._value.get() == 1.0
