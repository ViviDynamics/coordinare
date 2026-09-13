"""Keeping a failed turn's work on the branch (#393).

Everything needed to resume a card already exists. The branch name is a pure
function of the card (``coordinare/{card_id}/{slug}``) and ``_resolve_branch``
preserves an existing branch rather than clobbering it, so a container can die
at any point and the next one clones the same branch.

None of that helped, because nothing was left on it. In ``driver._feature`` the
commit sits AFTER the red phase::

    529  outcome = await _red_phase(...)   # raises MilestoneFailed at :485
    537  await _commit(ctx, outcome.changed, "test(#N): failing tests for ...")

and ``ctx.push()`` sits upstream of the handler that catches the exception. A
tests turn that wrote a spec and failed its red judgement therefore discarded
the file and pushed nothing.

Card #160 ran twice, six minutes of writing each time, and produced the
IDENTICAL verdict -- because both attempts started from an empty branch and
rewrote the same file. There was no memory between them, so repetition was
guaranteed rather than merely likely.

What the next container gets is the WORKING TREE: the partial file is on the
branch, so the next attempt edits it instead of deriving it from nothing.

It deliberately does NOT get a spec-171 resume credit. Those rules read
``PRIOR_RUN_PREFIXES`` and :data:`SALVAGE_PREFIX` is outside that set on
purpose, because salvaged work is by definition UNVALIDATED -- the milestone
failed. Card #160 failed with a spec that passed the moment it was written; if
resume counted that commit, the next run would read the milestone as ``done``
and skip it, turning an honest failure into a silent false green. Unfinished is
not the same as finished, and only one of the two may skip a milestone.

Salvaging is best effort by design. It runs on a path that is already reporting
failure, so an exception here would replace an honest ``partial_progress`` with
a crash and lose the reason an operator needs. The caller enforces that; see
``driver.run_milestone``.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

__all__ = ["SALVAGE_PREFIX", "salvage_failed_work", "salvage_message", "should_salvage"]

#: Marks the commit as unfinished so the next turn knows it is repairing rather
#: than starting, and so a human reading the branch is not misled into thinking
#: the milestone landed.
SALVAGE_PREFIX = "wip"

_MAX_REASON_CHARS = 1200

# Mirrors driver._is_build_artifact. A turn whose only output is a cache
# directory produced nothing worth carrying, and committing it would still move
# the head -- which would make the #390 budget read progress that did not happen.
_ARTIFACT_MARKERS = ("__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
                     "node_modules/", ".tox/")
_ARTIFACT_SUFFIXES = (".pyc", ".pyo")


def _is_artifact(path: str) -> bool:
    return path.endswith(_ARTIFACT_SUFFIXES) or any(m in path for m in _ARTIFACT_MARKERS)


def should_salvage(paths: dict[str, str] | None) -> bool:
    """Whether *paths* contains anything worth committing."""
    if not paths:
        return False
    return any(not _is_artifact(p) for p in paths)


def salvage_message(issue_number: int, reason: str | None) -> str:
    """The commit message the next turn reads off its own branch.

    Carries the reason, because an attempt that cannot see WHY the last one
    failed re-derives the same file and reproduces the same verdict -- which is
    precisely what card #160 did twice.
    """
    text = " ".join(str(reason or "").split())[:_MAX_REASON_CHARS]
    issue = f"(#{issue_number})" if issue_number else ""
    headline = f"{SALVAGE_PREFIX}{issue}: milestone did not complete; work kept for the next attempt"
    if not text:
        return headline + "\n\nNo reason was reported."
    return f"{headline}\n\nWhy it stopped: {text}"


async def salvage_failed_work(
    ctx: Any,
    *,
    since_sha: str,
    reason: str | None,
    _commit_paths: Any = None,
    _changed_paths_since: Any = None,
) -> bool:
    """Commit and push whatever this run produced. Returns whether it landed.

    The push matters as much as the commit: the next container clones from the
    REMOTE, so a local-only commit is invisible to exactly the case this exists
    for.
    """
    from performer.workflows.implementer import commits as git

    commit_paths = _commit_paths or git.commit_paths
    changed_since = _changed_paths_since or git.changed_paths_since
    workspace = ctx.workspace

    try:
        paths = await changed_since(workspace, since_sha)
    except Exception as exc:  # noqa: BLE001 - best effort on an already-failing path
        log.warning("implementer.salvage_scan_failed", error=str(exc)[:200])
        return False

    if not should_salvage(paths):
        log.info("implementer.salvage_nothing_to_keep", paths=len(paths or {}))
        return False

    keep = sorted(p for p in paths if not _is_artifact(p))
    try:
        sha = await commit_paths(workspace, keep, salvage_message(
            int(getattr(ctx, "issue_number", 0) or 0), reason))
    except Exception as exc:  # noqa: BLE001
        log.warning("implementer.salvage_commit_failed", error=str(exc)[:200], files=len(keep))
        return False
    if not sha:
        log.info("implementer.salvage_commit_empty", files=len(keep))
        return False

    try:
        await ctx.push()
    except Exception as exc:  # noqa: BLE001 - a rejected push must not replace
        # the honest partial_progress report with a crash.
        log.warning("implementer.salvage_push_failed", error=str(exc)[:200], commit=sha[:8])
        return False

    log.info("implementer.salvage_pushed", commit=str(sha)[:8], files=len(keep),
             paths=keep[:10])
    return True
