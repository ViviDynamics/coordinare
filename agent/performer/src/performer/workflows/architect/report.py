"""Report (spec 165 FR-007, FR-008): the blueprint, its size, and proof the
architect wrote nothing.

The write-free property is an executed check (``git status --porcelain`` must
print nothing), recorded in the same ``ExecutedCheck`` shape 164 uses, not a
promise. A dirty tree fails the run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.survey import Survey
from performer.workflows.base import WorkflowMetrics

WRITE_FREE_COMMAND = "git status --porcelain"


class ArchitectWroteToTree(RuntimeError):
    """The working tree is dirty after the architect ran: a defect, never a plan."""


def blueprint_hash(blueprint: Blueprint) -> str:
    payload = json.dumps(blueprint.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def write_free_check(toolkit, workspace: Path) -> dict[str, Any]:
    result = await toolkit.run_command(WRITE_FREE_COMMAND, cwd=workspace, timeout_s=30)
    clean = result.exit_code == 0 and not (result.output_excerpt or "").strip()
    return {
        "command": WRITE_FREE_COMMAND,
        "exit_code": result.exit_code,
        "passed": clean,
        "dirty_paths": [line[3:] for line in (result.output_excerpt or "").splitlines() if line.strip()][:20],
    }


def build_report(
    blueprint: Blueprint,
    size: str,
    survey: Survey,
    check: dict[str, Any],
    metrics: WorkflowMetrics,
) -> dict[str, Any]:
    if not check.get("passed"):
        raise ArchitectWroteToTree(
            "the architect left the working tree dirty: " + ", ".join(check.get("dirty_paths") or ["(unknown)"])
        )
    return {
        "blueprint": {
            **blueprint.model_dump(mode="json"),
            "size": size,
            "blueprint_hash": blueprint_hash(blueprint),
            "created_at": datetime.now(UTC).isoformat(),
        },
        "size": size,
        "write_free_check": {**check, "refused_commands": survey.refused},
        "survey": [asdict(r) | {"output": r.output[:200]} for r in survey.records],
        "workflow_metrics": {
            "model_calls": metrics.model_calls,
            "truncation_retries": metrics.truncation_retries,
            "schema_reprompts": metrics.schema_reprompts,
            "step_durations_ms": dict(metrics.step_durations_ms),
        },
    }
