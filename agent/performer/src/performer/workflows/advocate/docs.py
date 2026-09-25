"""Advocate documentation reading (spec 173).

Read from the working copy, not over the API. Every performer run clones the
repository before any role branching, so the files the old service fetched one
GraphQL call at a time are already on disk. Reading them locally costs no rate
limit and, more importantly, makes the grounding gate honest: the documents an
answer may cite are documents this run physically holds.

416: a ``doc_sources`` entry may be a file, a directory or a glob, and an
empty file is not documentation at all -- an empty README cannot support a
citation, so it is recorded unread.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import structlog

from performer.workflows.advocate.models import DocumentRead

log = structlog.get_logger(__name__)

#: A single document larger than this is truncated rather than dropped. A
#: 500KB generated reference would otherwise crowd out every other source.
_MAX_DOC_CHARS = 40_000

#: A directory or glob source expands to at most this many files. A repo with
#: a huge generated tree must not turn one entry into a thousand reads.
_MAX_FILES_PER_SOURCE = 200


def _read_file(rel: str, target: Path) -> DocumentRead:
    content = target.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        # 416: an empty document is not documentation, so it cannot support a
        # citation. It is recorded unread, like an absent file.
        return DocumentRead(path=rel, content=content, read=False)
    if len(content) > _MAX_DOC_CHARS:
        content = content[:_MAX_DOC_CHARS] + "\n[truncated]"
    return DocumentRead(path=rel, content=content, read=True)


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
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if ".." in parts:
            log.warning("advocate.doc_outside_repo", path=rel)
            documents.append(DocumentRead(path=rel, read=False))
            continue
        if any(ch in rel for ch in "*?["):
            # A glob: every match is a document in its own right, so citations
            # resolve against the file's own path rather than the pattern.
            try:
                matches = sorted(
                    p for p in base.glob(rel)
                    # is_file() follows symlinks, so a link inside the
                    # checkout pointing at /etc/passwd would be read and
                    # quoted into a comment: the resolved target must stay
                    # under the root.
                    if p.resolve().is_relative_to(base) and p.is_file()
                )
            except (OSError, ValueError, RuntimeError):
                matches = []
            if len(matches) > _MAX_FILES_PER_SOURCE:
                log.warning("advocate.doc_source_truncated", path=rel, files=len(matches))
                matches = matches[:_MAX_FILES_PER_SOURCE]
            documents.extend(
                _read_file(match.relative_to(base).as_posix(), match) for match in matches
            )
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
        if target.is_dir():
            children = sorted(
                p for p in target.rglob("*")
                # Same containment the glob branch applies: a symlink inside
                # the directory must not be followed outside the checkout.
                if p.resolve().is_relative_to(base) and p.is_file()
            )
            if len(children) > _MAX_FILES_PER_SOURCE:
                log.warning("advocate.doc_source_truncated", path=rel, files=len(children))
                children = children[:_MAX_FILES_PER_SOURCE]
            documents.extend(
                _read_file(child.relative_to(base).as_posix(), child) for child in children
            )
            continue
        try:
            documents.append(_read_file(rel, target))
        except (OSError, UnicodeDecodeError):
            documents.append(DocumentRead(path=rel, read=False))
            continue
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
