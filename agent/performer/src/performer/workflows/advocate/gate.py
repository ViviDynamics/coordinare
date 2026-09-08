"""Advocate gate (spec 173): pure rules deciding what may be posted.

The discipline is specs 169, 170 and 172 applied to a new surface: a claim
survives only when its evidence is in material the run actually read. Here the
claim is an answer to a stranger on a public issue, and the evidence is the
documentation file it cites.

Every rule in this module is a decision to speak in public on the project's
behalf, so every one is mutation-tested.
"""
from __future__ import annotations

import re

from performer.workflows.advocate.models import (
    ANSWERABLE_KINDS,
    Classification,
    DocumentRead,
)

#: Backticked spans in the answer, e.g. "Based on `README.md`:".
_BACKTICKED = re.compile(r"`([^`\n]{1,200})`")

#: Only a repository-shaped token counts as a citation. Spec 171 learned this
#: the expensive way: counting every backticked span made routes (`/charge`),
#: decorators (`@app.route`) and calls (`charge()`) into "missing citations"
#: and threw away good work. A citation must look like a file.
_PATH_LIKE = re.compile(
    r"^[\w][\w./-]*\.(?:md|markdown|rst|txt|py|rb|js|ts|tsx|jsx|go|rs|java|"
    r"yaml|yml|toml|json|cfg|ini|sh|sql|html|css)$",
    re.IGNORECASE,
)


def path_like_tokens(answer: str) -> list[str]:
    """Backticked spans in *answer* that are shaped like repository files."""
    out: list[str] = []
    for span in _BACKTICKED.findall(answer or ""):
        token = span.strip()
        if _PATH_LIKE.match(token) and token not in out:
            out.append(token)
    return out


def undeclared_citations(answer: str, documents: list[DocumentRead]) -> list[str]:
    """File-shaped tokens the answer names that this run never read.

    The declared ``cited_documents`` list is not the only way an answer can
    point at a file: a model can declare README.md honestly and then, in prose,
    attribute a claim to a config file that does not exist. Both routes are
    checked, because a reader believes the prose, not the metadata.
    """
    known = {d.path for d in documents if d.read}
    return [t for t in path_like_tokens(answer) if t not in known]


def claimed_paths(classification: Classification) -> list[str]:
    """What the model says it cited, deduplicated and stripped."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in classification.cited_documents:
        path = str(raw or "").strip().strip("`")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def unknown_citations(classification: Classification, documents: list[DocumentRead]) -> list[str]:
    """Claimed documents this run never read. Any of these fails the answer."""
    known = {d.path for d in documents if d.read}
    return [p for p in claimed_paths(classification) if p not in known]


def answer_is_grounded(
    classification: Classification,
    documents: list[DocumentRead],
) -> tuple[bool, str]:
    """Whether this answer may be posted, and why not when it may not.

    Three conditions, all required:

    * the kind is one an answer is allowed for at all,
    * at least one document is cited, because an uncited answer is the model
      speaking from its own memory of a project it has never seen,
    * every cited document is one this run read.
    """
    if classification.classification not in ANSWERABLE_KINDS:
        return False, f"an answer is not permitted for {classification.classification}"
    if not (classification.answer or "").strip():
        return False, "no answer text"
    claimed = claimed_paths(classification)
    if not claimed:
        return False, "the answer cites no documentation"
    unknown = unknown_citations(classification, documents)
    if unknown:
        return False, f"cites documents this run never read: {', '.join(sorted(unknown))}"
    undeclared = undeclared_citations(classification.answer or "", documents)
    if undeclared:
        return False, (
            f"the answer text names files this run never read: {', '.join(sorted(undeclared))}"
        )
    return True, ""


def accept_classification(
    classification: Classification,
    sent_ids: set[str],
) -> bool:
    """Discard a judgement about an issue this run did not send.

    A model that answers about an issue nobody asked about is either confused
    or echoing its context, and acting on it would comment on an unrelated
    issue.
    """
    return classification.issue_id in sent_ids


def confident_enough(classification: Classification, threshold: float) -> bool:
    """The confidence gate, which applies only to answerable kinds.

    Preserved from the behaviour this replaces: a complaint escalates whatever
    the confidence, and a bug report is triaged whatever the confidence, so the
    threshold has never applied to them.
    """
    if classification.classification not in ANSWERABLE_KINDS:
        return True
    return classification.confidence >= threshold
