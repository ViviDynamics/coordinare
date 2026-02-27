from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class CoordinareMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.cards_processed_total = Counter(
            "coordinare_cards_processed_total",
            "Total number of cards processed",
            registry=self.registry,
        )
        self.card_cycle_seconds = Histogram(
            "coordinare_card_cycle_seconds",
            "Time from card pickup to Done",
            registry=self.registry,
            buckets=(300, 600, 1800, 3600),
        )
        self.notifications_dispatched_total = Counter(
            "coordinare_notifications_dispatched_total",
            "Notifications successfully delivered",
            labelnames=("event_type", "channel"),
            registry=self.registry,
        )
        self.notifications_failed_total = Counter(
            "coordinare_notifications_failed_total",
            "Notifications that failed after all retries",
            labelnames=("channel",),
            registry=self.registry,
        )
        self.notifications_rate_limited_total = Counter(
            "coordinare_notifications_rate_limited_total",
            "Notifications dropped due to rate limiting",
            labelnames=("channel",),
            registry=self.registry,
        )
        self.notifications_deduplicated_total = Counter(
            "coordinare_notifications_deduplicated_total",
            "Notifications suppressed by deduplication",
            labelnames=("channel",),
            registry=self.registry,
        )
        self.agent_dispatch_seconds = Histogram(
            "coordinare_agent_dispatch_seconds",
            "Time to dispatch card to agent via SSH",
            registry=self.registry,
            buckets=(5, 10, 30),
        )
        self.errors_total = Counter(
            "coordinare_errors_total",
            "Error count by category",
            labelnames=("category",),
            registry=self.registry,
        )
        self.board_poll_seconds = Gauge(
            "coordinare_board_poll_seconds",
            "Time to complete a board poll",
            registry=self.registry,
        )
        self.up = Gauge(
            "coordinare_up",
            "Whether the coordinare daemon is running",
            registry=self.registry,
        )
        self.state_write_duration_seconds = Histogram(
            "coordinare_state_write_duration_seconds",
            "Duration of atomic state file write operations",
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.5, 1.0),
        )
        self.state_write_failures_total = Counter(
            "coordinare_state_write_failures_total",
            "Total number of state write failures",
            registry=self.registry,
        )
        self.state_last_written_timestamp = Gauge(
            "coordinare_state_last_written_timestamp",
            "Unix timestamp of the last successful state write",
            registry=self.registry,
        )

        # Resilience metrics (spec 005)
        self.circuit_breaker_state = Gauge(
            "coordinare_circuit_breaker_state",
            "Current circuit breaker state per service (1=active, 0=inactive)",
            labelnames=("service", "state"),
            registry=self.registry,
        )
        self.service_retries_total = Counter(
            "coordinare_service_retries_total",
            "Total retry attempts per service and action",
            labelnames=("service", "action"),
            registry=self.registry,
        )
        self.service_calls_total = Counter(
            "coordinare_service_calls_total",
            "Total service call outcomes",
            labelnames=("service", "action", "outcome"),
            registry=self.registry,
        )

    def observe_error(self, category: str) -> None:
        self.errors_total.labels(category=category).inc()

    def render(self) -> str:
        return generate_latest(self.registry).decode("utf-8")


METRICS = CoordinareMetrics()
