"""Documenter commit step (spec 171 FR-012): one commit through the existing path.

Writes the surviving pages, the generated README and the pointer sections and
removes accepted retirements through ``performer.workspace.commit_files``
(it stages, commits and pushes). Injectable ``committer`` for tests.

There is no stray-path revert: the workflow only writes documentation paths
(``is_doc_path``) and the run record lists every file it wrote, so anything
that goes wrong surfaces in the report rather than being silently reverted
(415: the revert path was dead code -- nothing ever populated it).
"""
from __future__ import annotations

from typing import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

__all__ = ["commit_docs", "Committer"]


Committer = Callable[..., Awaitable[list[str]]]

async def commit_docs(stand, score, *, files: list[dict[str, str]], deletions: list[str], message: str, committer: Committer | None = None) -> list[str]:
    """Commit and push once; returns the changed paths. Raises on failure (the caller holds)."""
    if not files and not deletions:
        return []
    if committer is None:
        from performer.workspace import commit_files as committer  # noqa: PLC0415 - keeps the workflow importable without git deps
    changed = await committer(stand, files, message, deletions=deletions, score=score)
    log.info("documenter.committed", files=len(files), deletions=len(deletions))
    return list(changed)
