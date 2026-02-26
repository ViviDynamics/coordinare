from __future__ import annotations

from coordinare.metrics import CoordinareMetrics


def test_observe_notification_increments_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.observe_notification("slack", "success")
    metrics.observe_notification("email", "failure")

    assert metrics.notifications_total.labels(channel="slack", status="success")._value.get() == 1.0
    assert metrics.notifications_total.labels(channel="email", status="failure")._value.get() == 1.0


def test_observe_error_increments_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.observe_error("graph_execution")

    assert metrics.errors_total.labels(category="graph_execution")._value.get() == 1.0
