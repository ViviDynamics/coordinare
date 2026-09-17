"""Step 2: capture the pre-change state (spec 164).

A pull request is a CHANGE, so QA's question is about a delta. Today's QA looks
at one screenshot and asks "is this right?" with no baseline, which is why it is
structurally blind to regressions: one image with nothing to compare against
cannot show that something disappeared.

Uses ``git worktree`` at the merge-base (R4). The head tree must stay intact --
both trees are needed alive at once -- and a destructive checkout would strand
uncommitted state if a later step raised.

Skipped entirely when the plan contains no visual or flow checks. The second
boot is the main cost this design adds, and paying it for a pure-command plan
buys nothing.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from performer.workflows.models import Observation
from performer.workflows.qa.boot import rebase_origin, resolve_target
from performer.workflows.qa.models import TestPlan

log = structlog.get_logger(__name__)


async def run_baseline_step(
    toolkit,
    plan: TestPlan,
    *,
    workspace: Path,
    merge_base: str,
    worktree_dir: Path,
    base_url: str | None = None,
    boot_base=None,
) -> dict[str, list[Observation]]:
    """Return per-surface observations for the pre-change state.

    An empty dict means the baseline was skipped, which is a legitimate outcome
    rather than a failure.
    """
    if not plan.needs_baseline():
        log.info("qa.baseline.skipped", reason="no visual or flow checks planned")
        return {}

    add = await toolkit.run_command(
        f"git worktree add --detach {worktree_dir} {merge_base}",
        cwd=workspace,
    )
    if not add.passed:
        # Fail closed: without a baseline the delta is unknowable, and reporting
        # "no regressions" from a missing comparison is exactly the false
        # reassurance this step exists to prevent.
        log.warning("qa.baseline.worktree_failed", exit_code=add.exit_code)
        raise RuntimeError(
            f"could not create baseline worktree at {merge_base}: "
            f"exit {add.exit_code}",
        )

    # Boot the BASE app from the worktree. Without this, dom_snapshot hits
    # whatever is already serving -- the HEAD app -- so before and after are the
    # same page and the delta is always empty. Regression detection depends
    # entirely on this being a different, older process.
    base_boot = boot_base(worktree_dir) if boot_base else None
    base_origin = await base_boot.ensure_serving(toolkit) if base_boot else None
    if base_boot is not None and base_origin is None:
        base_boot.shutdown()
        # Carry the boot's own diagnosis: a merge-base that does not build, a
        # crash, and a slow start are different fixes, and "never came up"
        # said the same thing for all three.
        why = getattr(base_boot, "failure_reason", None) or "it never came up"
        raise RuntimeError(
            f"the base-commit application could not be compared against: {why}. "
            "No before/after comparison is possible.",
        )

    baseline: dict[str, list[Observation]] = {}
    try:
        # The observation targets are the declared surfaces PLUS every visual
        # check's own navigation. A visual check may goto a target the
        # planner never declared as a surface; with declared surfaces empty
        # that left the before map empty, skipped the post-change observe
        # entirely, and let the screenshot/exit code support a visual pass
        # with no before/after comparison at all (411 round-eight review).
        # The post-change observe iterates the before map, so a derived
        # target is compared on both sides for free.
        surfaces = list(plan.surfaces)
        for check in plan.checks:
            if check.kind != "visual":
                continue
            for step in check.steps:
                if step.action == "goto" and step.target:
                    if step.target not in surfaces:
                        surfaces.append(step.target)
        for surface in surfaces:
            # Read from the BASE app's origin, falling back to the planned base
            # URL only when no separate base app was booted.
            # Resolve a relative surface against the planned URL, then move it
            # onto the BASE app's origin. Both steps are needed: surfaces may be
            # relative or absolute, and either way the baseline must read from
            # the older process.
            resolved = resolve_target(surface, base_url) or surface
            resolved = rebase_origin(resolved, base_origin)
            elements = await toolkit.dom_snapshot(resolved)
            baseline[surface] = [
                Observation(
                    kind=e.get("kind", "other"),
                    position=i,
                    label=e.get("label"),
                )
                for i, e in enumerate(elements)
            ]
    finally:
        if base_boot is not None:
            base_boot.shutdown()
    return baseline


async def cleanup_worktree(toolkit, *, workspace: Path, worktree_dir: Path) -> None:
    """Best-effort removal. A leftover worktree is untidy, not dangerous."""
    try:
        await toolkit.run_command(
            f"git worktree remove --force {worktree_dir}", cwd=workspace,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("qa.baseline.cleanup_failed", error=str(exc))
