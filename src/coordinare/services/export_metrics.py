from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter

from coordinare.metrics import METRICS

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

STAGES = frozenset(
    {
        "implementing",
        "reviewing",
        "security",
        "qa",
        "documenting",
        "architecting",
        "advocate",
        "assessing",
        "closing_review",
        "env_bootstrap",
    },
)
OUTCOMES = frozenset({"completed", "blocked", "system_error", "redispatched"})


class ExportMetrics:
    """Process counters with fixed, bounded labels; no card or run identifiers."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.started = Counter(
            "coordinare_runs_started_total",
            "Accepted performer runs by stage",
            ["stage"],
            registry=registry,
        )
        self.finished = Counter(
            "coordinare_runs_finished_total",
            "Runs leaving the monitor by stage and outcome",
            ["stage", "outcome"],
            registry=registry,
        )
        self.bootstrap = Counter(
            "coordinare_bootstrap_outcomes_total",
            "Completed bootstrap outcomes",
            ["outcome"],
            registry=registry,
        )
        self.written = Counter(
            "coordinare_event_store_written_total",
            "Event rows committed this process",
            registry=registry,
        )
        self.dropped = Counter(
            "coordinare_event_store_dropped_total",
            "Event rows lost before commit",
            registry=registry,
        )
        self.pruned = Counter(
            "coordinare_event_store_pruned_total",
            "Expired or oversized event rows removed",
            registry=registry,
        )
        self.export_dropped = Counter(
            "coordinare_export_dropped_total", "Rows lost during optional export", registry=registry,
        )
        self.export_failed = Counter(
            "coordinare_export_failed_total", "Failed export send attempts", registry=registry,
        )

    def run_started(self, stage: str) -> None:
        self.started.labels(stage=stage if stage in STAGES else "unknown").inc()

    def run_finished(self, stage: str, outcome: str) -> None:
        self.finished.labels(
            stage=stage if stage in STAGES else "unknown",
            outcome=outcome if outcome in OUTCOMES else "unknown",
        ).inc()

    def observe(self, row: dict[str, Any]) -> None:
        if row.get("source") == "bootstrap" and row.get("kind") == "env_cache.bootstrap_complete":
            data = json.loads(row["data"])
            if isinstance(data, dict) and isinstance(data.get("success"), bool):
                self.bootstrap.labels(outcome="succeeded" if data["success"] else "failed").inc()


EXPORT_METRICS = ExportMetrics(METRICS.registry)
