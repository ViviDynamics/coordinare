"""Advocate documentation reading (spec 173).

Read from the working copy, not over the API. Every performer run clones the
repository before any role branching, so the files the old service fetched one
GraphQL call at a time are already on disk. Reading them locally costs no rate
limit and, more importantly, makes the grounding gate honest: the documents an
answer may cite are documents this run physically holds.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from performer.workflows.advocate.models import DocumentRead

log = structlog.get_logger(__name__)

#: A single document larger than this is truncated rather than dropped. A
#: 500KB generated reference would otherwise crowd out every other source.
_MAX_DOC_CHARS = 40_000


def read_documents(root: Path, doc_sources: list[str]) -> list[DocumentRead]:
    """Read each configured path, recording absent ones rather than skipping.

    Paths are resolved inside *root* and anything escaping it is refused: a
    ``doc_sources`` entry of ``../../etc/passwd`` is a misconfiguration at best,
    and its content would end up quoted into a public issue comment.
    """
    base = root.resolve()
    documents: list[DocumentRead] = []
    for source in doc_sources:
        rel = str(source or "").strip()
        if not rel:
            continue
        try:
            target = (base / rel).resolve()
        except (OSError, RuntimeError):
            documents.append(DocumentRead(path=rel, read=False))
            continue
        if not target.is_relative_to(base):
            log.warning("advocate.doc_outside_repo", path=rel)
            documents.append(DocumentRead(path=rel, read=False))
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            documents.append(DocumentRead(path=rel, read=False))
            continue
        if len(content) > _MAX_DOC_CHARS:
            content = content[:_MAX_DOC_CHARS] + "\n[truncated]"
        documents.append(DocumentRead(path=rel, content=content, read=True))
    return documents


def readable(documents: list[DocumentRead]) -> list[DocumentRead]:
    """Only the documents actually read. This is the gate's allow-list."""
    return [d for d in documents if d.read]


def render(documents: list[DocumentRead]) -> str:
    """The documentation value handed to the model.

    This contains documentation and nothing else. The persona travels in the
    instruction argument: mixing them is what made the previous implementation
    ungateable, because an answer could "cite" a heading that was an
    instruction rather than a file.
    """
    parts: list[str] = []
    for doc in readable(documents):
        parts.append(f"--- {doc.path} ---\n{doc.content}")
    return "\n\n".join(parts)
