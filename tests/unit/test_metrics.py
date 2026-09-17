from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from coordinare.metrics import CoordinareMetrics, _coordinare_version


def test_notifications_dispatched_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.notifications_dispatched_total.labels(event_type="card_transition", channel_name="slack-ops").inc()
    assert metrics.notifications_dispatched_total.labels(
        event_type="card_transition", channel_name="slack-ops",
    )._value.get() == 1.0


def test_notifications_failed_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.notifications_failed_total.labels(channel_name="slack-ops").inc()
    assert metrics.notifications_failed_total.labels(channel_name="slack-ops")._value.get() == 1.0


def test_observe_error_increments_counter() -> None:
    metrics = CoordinareMetrics()
    metrics.observe_error("graph_execution")

    assert metrics.errors_total.labels(symphony="__default__", category="graph_execution")._value.get() == 1.0


def test_coordinare_version_returns_dev_when_package_not_installed() -> None:
    """_coordinare_version() returns 'dev' when the package is not installed."""
    with patch("coordinare.metrics.version", side_effect=PackageNotFoundError("coordinare")):
        result = _coordinare_version()
    assert result == "dev"


def test_card_tokens_total_exists_and_increments() -> None:
    """034: card_tokens_total counter exists and can be incremented with a role label."""
    metrics = CoordinareMetrics()
    metrics.card_tokens_total.labels(role="implementing").inc(500)
    assert metrics.card_tokens_total.labels(role="implementing")._value.get() == 500.0


def test_card_cost_estimate_dollars_exists_and_sets() -> None:
    """034: card_cost_estimate_dollars gauge exists and can be set."""
    metrics = CoordinareMetrics()
    metrics.card_cost_estimate_dollars.set(1.23)
    assert metrics.card_cost_estimate_dollars._value.get() == 1.23
