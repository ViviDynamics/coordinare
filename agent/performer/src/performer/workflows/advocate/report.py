"""The advocate run's report (spec 173)."""
from __future__ import annotations

from typing import Any

from performer.workflows.advocate.models import AdvocateRecord


def build_report(record: AdvocateRecord, metrics: Any) -> dict[str, Any]:
    """The dict main.py maps onto a PerformerResponse."""
    return {
        "advocate": record.model_dump(mode="json"),
        "write_free_check": record.write_free_check,
        "workflow_metrics": {
            "model_calls": getattr(metrics, "model_calls", 0),
            "schema_reprompts": getattr(metrics, "schema_reprompts", 0),
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
        },
    }
