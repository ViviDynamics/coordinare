"""Security report (spec 170 FR-017, FR-018): the record with the executed write-free check."""
from __future__ import annotations

from typing import Any

from performer.workflows.reviewer.report import write_free_check
from performer.workflows.security.models import SecurityRecord

__all__ = ["SecurityWroteToTree", "build_report", "write_free_check"]


class SecurityWroteToTree(RuntimeError):
    """The security workflow left the working tree dirty (FR-017)."""


def build_report(record: SecurityRecord, check: dict[str, Any], metrics: Any) -> dict[str, Any]:
    if not check.get("passed"):
        raise SecurityWroteToTree("the security workflow left the working tree dirty: " + ", ".join(check.get("dirty_paths") or ["(unknown)"]))
    return {
        "security": record.model_dump(mode="json"),
        "write_free_check": dict(check),
        "workflow_metrics": {
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
            "model_calls": getattr(metrics, "model_calls", 0),
            "truncation_retries": getattr(metrics, "truncation_retries", 0),
            "schema_reprompts": getattr(metrics, "schema_reprompts", 0),
            "commands_run": getattr(metrics, "commands_run", 0),
        },
    }
