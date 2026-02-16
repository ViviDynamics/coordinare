from __future__ import annotations

from typing import Literal

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
        self.notifications_total = Counter(
            "coordinare_notifications_total",
            "Notifications sent by channel and status",
            labelnames=("channel", "status"),
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

    def observe_notification(self, channel: str, status: Literal["success", "failure"]) -> None:
        self.notifications_total.labels(channel=channel, status=status).inc()

    def observe_error(self, category: str) -> None:
        self.errors_total.labels(category=category).inc()

    def render(self) -> str:
        return generate_latest(self.registry).decode("utf-8")


METRICS = CoordinareMetrics()
