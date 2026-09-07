"""Closer report (spec 172 FR-010, FR-012)."""
from __future__ import annotations

from typing import Any

from performer.workflows.closer.models import ClosingRecord

__all__ = ["build_report"]


def build_report(record: ClosingRecord, metrics: Any) -> dict[str, Any]:
    return {
        "closing": record.model_dump(mode="json"),
        "workflow_metrics": {
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
            "model_calls": getattr(metrics, "model_calls", 0),
            "truncation_retries": getattr(metrics, "truncation_retries", 0),
            "schema_reprompts": getattr(metrics, "schema_reprompts", 0),
        },
    }
