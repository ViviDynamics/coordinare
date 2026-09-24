"""Report (spec 165 FR-007, FR-008): the blueprint, its size, and proof the
architect wrote nothing.

The write-free property is an executed check (``git status --porcelain`` must
print nothing), recorded in the same ``ExecutedCheck`` shape 164 uses, not a
promise. A dirty tree fails the run — 417 adds a pre-run baseline, so dirt that
already existed when the architect started is reported but does not fail it.

417 also validates the blueprint's paths against the tree (exist, or declared
``new:``) and runs the 396 degeneracy guards on the plan itself, closing the
gap between the prose and workflow paths.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from performer.degeneracy import classify_text
from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.survey import Survey
from performer.workflows.base import WorkflowMetrics

WRITE_FREE_COMMAND = "git status --porcelain"

PLAN_SIZE_CAP = 131_072
PLAN_TOP_LINE_FRACTION = 0.35
PLAN_MIN_REPETITION_LINES = 40
PLAN_UNIQUE_LINE_RATIO_MAX = 0.15
_NEW_PATH_PREFIX = "new:"


class ArchitectWroteToTree(RuntimeError):
    """The working tree is dirty after the architect ran: a defect, never a plan."""


class BlueprintPathError(RuntimeError):
    """A blueprint path is hallucinated, absolute, or escapes the workspace."""


class DegeneratePlan(RuntimeError):
    """The blueprint plan is oversized or degenerate (417, closing 396)."""


def blueprint_hash(blueprint: Blueprint) -> str:
    payload = json.dumps(blueprint.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _porcelain_paths(output: str, limit: int = 20) -> list[str]:
    return [line[3:] for line in (output or "").splitlines() if line.strip()][:limit]


async def tree_state(toolkit, workspace: Path) -> list[str]:
    """Snapshot the dirty paths BEFORE the survey runs (417).

    Uncapped to 1000: the baseline must know every pre-existing path, or dirt
    beyond a truncated snapshot would be misattributed to the architect.
    """
    result = await toolkit.run_command(WRITE_FREE_COMMAND, cwd=workspace, timeout_s=30)
    return _porcelain_paths(result.output_excerpt or "", limit=1000)


async def write_free_check(toolkit, workspace: Path, baseline: list[str] | None = None) -> dict[str, Any]:
    """Refuse only dirt the architect itself produced (417).

    *baseline* is the :func:`tree_state` snapshot taken before any step ran.
    Paths already dirty then are reported as ``pre_existing`` and never fail
    the check; with no baseline, any dirt fails, as before.
    """
    result = await toolkit.run_command(WRITE_FREE_COMMAND, cwd=workspace, timeout_s=30)
    paths = _porcelain_paths(result.output_excerpt or "")
    base = set(baseline or [])
    pre_existing = [p for p in paths if p in base]
    dirty = [p for p in paths if p not in base]
    clean = result.exit_code == 0 and not dirty
    return {
        "command": WRITE_FREE_COMMAND,
        "exit_code": result.exit_code,
        "passed": clean,
        "dirty_paths": dirty,
        "pre_existing": pre_existing,
    }


def invalid_blueprint_paths(blueprint: Blueprint, workspace: Path) -> list[str]:
    """Paths the blueprint cites that neither exist nor are declared ``new:`` (417).

    Absolute paths and ``..`` escapes are refused outright. A ``new:`` prefix
    declares a path the architect intends the plan to create, so it validates
    without existing yet. Returns the invalid raw values, deduplicated, order
    preserved.
    """
    root = Path(workspace)
    invalid: list[str] = []
    for raw in _blueprint_paths(blueprint):
        if _path_is_invalid(raw, root):
            if raw not in invalid:
                invalid.append(raw)
    return invalid


def _path_is_invalid(raw: str, root: Path) -> bool:
    value = raw.strip()
    if value.startswith(_NEW_PATH_PREFIX):
        value = value[len(_NEW_PATH_PREFIX):].strip()
        return not value
    if not value:
        return True
    if value.startswith("/") or value.startswith("~"):
        return True
    candidate = (root / value).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        return True
    return not candidate.exists()


def _blueprint_paths(blueprint: Blueprint) -> list[str]:
    paths = [module.path for module in blueprint.modules]
    paths.extend(path for milestone in blueprint.milestones for path in milestone.scope)
    return paths


def enforce_blueprint_paths(blueprint: Blueprint, workspace: Path) -> None:
    invalid = invalid_blueprint_paths(blueprint, workspace)
    if invalid:
        raise BlueprintPathError(
            "blueprint cites paths that do not exist in the tree "
            "(declare new files with a 'new:' prefix): " + ", ".join(invalid[:8]),
        )


def _plan_lines(blueprint: Blueprint) -> str:
    lines: list[str] = [blueprint.summary]
    lines.extend(
        line for milestone in blueprint.milestones
        for line in (milestone.goal, milestone.done_when, *milestone.scope)
    )
    lines.extend(line for module in blueprint.modules for line in (module.path, module.note))
    lines.extend(
        line for change in blueprint.data_model.changes
        for line in (f"{change.kind} {change.name}", change.note)
    )
    lines.extend(
        line for interface in blueprint.interfaces
        for line in (f"{interface.kind} {interface.name}", interface.contract)
    )
    lines.extend(blueprint.risks)
    lines.extend(
        f"{criterion.surface} {criterion.action} {criterion.expected} [{criterion.kind}]"
        for criterion in blueprint.criteria
    )
    lines.extend(f"{doc.topic} ({doc.location}): {doc.say}" for doc in blueprint.docs)
    return "\n".join(lines)


def enforce_plan_guards(
    blueprint: Blueprint,
    *,
    size_cap: int = PLAN_SIZE_CAP,
    top_line_fraction: float = PLAN_TOP_LINE_FRACTION,
    min_repetition_lines: int = PLAN_MIN_REPETITION_LINES,
    unique_line_ratio_max: float = PLAN_UNIQUE_LINE_RATIO_MAX,
) -> None:
    """The 396 prose guards applied to the workflow plan (417).

    Refuses a blueprint whose rendered plan exceeds the size cap or is
    degenerate (one line repeated, or almost no unique lines). Keyword
    arguments are tunable so tests can tighten the size rule.
    """
    verdict = classify_text(
        _plan_lines(blueprint),
        size_cap=size_cap,
        top_line_fraction=top_line_fraction,
        min_repetition_lines=min_repetition_lines,
        unique_line_ratio_max=unique_line_ratio_max,
    )
    if verdict.degenerate:
        raise DegeneratePlan("blueprint plan refused: " + "; ".join(verdict.reasons))


def build_report(
    blueprint: Blueprint,
    size: str,
    survey: Survey,
    check: dict[str, Any],
    metrics: WorkflowMetrics,
) -> dict[str, Any]:
    if not check.get("passed"):
        raise ArchitectWroteToTree(
            "the architect left the working tree dirty: " + ", ".join(check.get("dirty_paths") or ["(unknown)"]),
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
