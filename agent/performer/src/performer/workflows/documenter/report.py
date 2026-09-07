"""Documenter report (spec 171 FR-013, FR-015)."""
from __future__ import annotations

from typing import Any

from performer.workflows.documenter.models import DocsRecord
from performer.workflows.reviewer.report import write_free_check

__all__ = ["build_report", "write_free_check"]


def build_report(record: DocsRecord, metrics: Any) -> dict[str, Any]:
    return {
        "docs": record.model_dump(mode="json"),
        "workflow_metrics": {
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
            "model_calls": getattr(metrics, "model_calls", 0),
            "truncation_retries": getattr(metrics, "truncation_retries", 0),
            "schema_reprompts": getattr(metrics, "schema_reprompts", 0),
            "commands_run": getattr(metrics, "commands_run", 0),
        },
    }
