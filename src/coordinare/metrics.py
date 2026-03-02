from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)


def _coordinare_version() -> str:
    try:
        return version("coordinare")
    except PackageNotFoundError:
        return "dev"


class CoordinareMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        # --- Board orchestrator metrics (spec 009 FR-001) ---
        self.cycles_completed_total = Counter(
            "coordinare_cycles_completed_total",
            "Total number of poll cycles completed successfully",
            registry=self.registry,
        )
        self.cards_processed_total = Counter(
            "coordinare_cards_processed_total",
            "Total number of cards processed by status",
            labelnames=("card_status",),
            registry=self.registry,
        )
        self.card_state_transitions_total = Counter(
            "coordinare_card_state_transitions_total",
            "Card state transition events by transition type",
            labelnames=("transition_type",),
            registry=self.registry,
        )
        self.cycle_duration_seconds = Histogram(
            "coordinare_cycle_duration_seconds",
            "Full poll cycle wall-clock duration",
            registry=self.registry,
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self.daemon_up = Gauge(
            "coordinare_daemon_up",
            "1 when daemon is running, 0 after shutdown",
            registry=self.registry,
        )

        # --- Config & build metrics (spec 009 FR-004) ---
        self.config_load_duration_seconds = Histogram(
            "coordinare_config_load_duration_seconds",
            "Time to load and validate configuration at startup",
            registry=self.registry,
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
        )
        self.build_info = Info(
            "coordinare_build",
            "Coordinare build metadata",
            registry=self.registry,
        )

        # --- Notification metrics (spec 006 emission; spec 009 defines names) ---
        self.notifications_dispatched_total = Counter(
            "coordinare_notifications_dispatched_total",
            "Notifications successfully delivered",
            labelnames=("event_type", "channel_name"),
            registry=self.registry,
        )
        self.notifications_failed_total = Counter(
            "coordinare_notifications_failed_total",
            "Notifications that failed after all retries",
            labelnames=("channel_name",),
            registry=self.registry,
        )
        self.notifications_rate_limited_total = Counter(
            "coordinare_notifications_rate_limited_total",
            "Notifications dropped due to rate limiting",
            labelnames=("channel_name",),
            registry=self.registry,
        )
        self.notifications_deduplicated_total = Counter(
            "coordinare_notifications_deduplicated_total",
            "Notifications suppressed by deduplication",
            labelnames=("channel_name",),
            registry=self.registry,
        )

        # --- Circuit breaker metrics (spec 005 emission; spec 009 defines names) ---
        self.circuit_breaker_trips_total = Counter(
            "coordinare_circuit_breaker_trips_total",
            "Circuit breaker trip events by service",
            labelnames=("service_name",),
            registry=self.registry,
        )
        self.circuit_breaker_state = Gauge(
            "coordinare_circuit_breaker_state",
            "Circuit breaker state: 0=closed, 1=half-open, 2=open",
            labelnames=("service_name",),
            registry=self.registry,
        )

        # --- Observability helpers (histogram for board polls, agent dispatch) ---
        self.card_cycle_seconds = Histogram(
            "coordinare_card_cycle_seconds",
            "Time from card pickup to Done",
            registry=self.registry,
            buckets=(300, 600, 1800, 3600),
        )
        self.agent_dispatch_seconds = Histogram(
            "coordinare_agent_dispatch_seconds",
            "Time to dispatch card to agent",
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

        # --- Advocate metrics (spec 007) ---
        self.advocate_issues_processed_total = Counter(
            "coordinare_advocate_issues_processed_total",
            "Advocate issues processed by action taken",
            labelnames=("action",),
            registry=self.registry,
        )
        self.advocate_issues_escalated_total = Counter(
            "coordinare_advocate_issues_escalated_total",
            "Advocate issues escalated to human by reason",
            labelnames=("reason",),
            registry=self.registry,
        )
        self.advocate_scan_duration_seconds = Histogram(
            "coordinare_advocate_scan_duration_seconds",
            "Time to complete one advocate scan cycle",
            registry=self.registry,
            buckets=(1, 5, 10, 30, 60),
        )

        # --- Resilience metrics (spec 005) ---
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

        self._initialize_zero_values()

    def _initialize_zero_values(self) -> None:
        """Pre-populate label combinations so /metrics shows zero values before any events."""
        # card_state_transitions_total
        for t in (
            "idle_to_dispatch",
            "dispatch_to_monitor",
            "monitor_to_merge",
            "monitor_to_blocked",
            "blocked_to_idle",
        ):
            self.card_state_transitions_total.labels(transition_type=t)

        # cards_processed_total
        for s in ("dispatched", "monitoring_agent", "monitoring_pr", "blocked", "merged", "idle"):
            self.cards_processed_total.labels(card_status=s)

        # notification counters
        for ch in ("slack", "email"):
            self.notifications_dispatched_total.labels(event_type="card_blocked", channel_name=ch)
            self.notifications_failed_total.labels(channel_name=ch)
            self.notifications_rate_limited_total.labels(channel_name=ch)
            self.notifications_deduplicated_total.labels(channel_name=ch)

        # circuit breaker
        for svc in ("github", "agent_ssh", "slack", "smtp"):
            self.circuit_breaker_trips_total.labels(service_name=svc)
            self.circuit_breaker_state.labels(service_name=svc)

    def observe_error(self, category: str) -> None:
        self.errors_total.labels(category=category).inc()

    def render(self) -> str:
        return generate_latest(self.registry).decode("utf-8")


METRICS = CoordinareMetrics()
