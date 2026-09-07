"""Reviewer report (spec 169 FR-011, FR-014, FR-016).

The report is ``{"review": ReviewRecord, "write_free_check": ..., "workflow_metrics": ...}``.
The write-free proof is executed (``git status --porcelain`` through the
toolkit, as the architect does): the reviewer holds a command runner for the
survey, so a structural proof is not enough. A dirty tree fails the round.
"""
from __future__ import annotations

from typing import Any

from performer.workflows.architect.report import write_free_check
from performer.workflows.reviewer.models import ReviewRecord

__all__ = ["ReviewerWroteToTree", "build_report", "write_free_check"]


class ReviewerWroteToTree(RuntimeError):
    """The reviewer left the working tree dirty (FR-014)."""


def build_report(record: ReviewRecord, check: dict[str, Any], metrics: Any) -> dict[str, Any]:
    if not check.get("passed"):
        raise ReviewerWroteToTree("the reviewer left the working tree dirty: " + ", ".join(check.get("dirty_paths") or ["(unknown)"]))
    return {
        "review": record.model_dump(mode="json"),
        "write_free_check": dict(check),
        "workflow_metrics": {
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
            "model_calls": getattr(metrics, "model_calls", 0),
            "truncation_retries": getattr(metrics, "truncation_retries", 0),
            "schema_reprompts": getattr(metrics, "schema_reprompts", 0),
            "commands_run": getattr(metrics, "commands_run", 0),
        },
    }
