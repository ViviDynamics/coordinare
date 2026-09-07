"""Documenter commit step (spec 171 FR-012): one commit through the existing path.

Writes the surviving pages, the generated README and the pointer sections, removes
accepted retirements, and calls ``performer.workspace.commit_files`` once (it
stages, commits and pushes). Anything the run touched outside the documentation
paths is reverted before the commit. Injectable ``committer`` for tests.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable

import structlog

from performer.workflows.documenter.plan import is_doc_path

log = structlog.get_logger(__name__)

__all__ = ["stray_paths", "commit_docs", "Committer"]

Committer = Callable[..., Awaitable[list[str]]]


def stray_paths(changed: Iterable[str], allowed: set[str]) -> list[str]:
    """Paths that changed but are neither planned writes nor documentation paths."""
    return sorted(p for p in changed if p not in allowed and not is_doc_path(p))


async def commit_docs(stand, score, *, files: list[dict[str, str]], deletions: list[str], message: str, committer: Committer | None = None) -> list[str]:
    """Commit and push once; returns the changed paths. Raises on failure (the caller holds)."""
    if not files and not deletions:
        return []
    if committer is None:
        from performer.workspace import commit_files as committer  # noqa: PLC0415 - keeps the workflow importable without git deps
    changed = await committer(stand, files, message, deletions=deletions, score=score)
    log.info("documenter.committed", files=len(files), deletions=len(deletions))
    return list(changed)
