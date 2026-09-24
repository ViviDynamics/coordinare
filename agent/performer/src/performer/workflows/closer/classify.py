"""Classification rules for review threads (spec 172).

Each rule is a pure function. The predicates are mutated in tests to verify
each rule is essential to the logic.
"""
from __future__ import annotations

from datetime import UTC, datetime

from performer.workflows.closer.models import Classification, Thread


def is_resolved(t: Thread) -> bool:
    """Resolved: GitHub explicitly marked it resolved."""
    return t.resolved


def is_stale(t: Thread) -> bool:
    """Stale: unresolved but the code it pointed at has changed."""
    return not t.resolved and t.outdated


def is_answered(t: Thread) -> bool:
    """Answered: unresolved, not outdated, and the last comment is by someone
    else later than the first comment.

    A thread whose last comment is the raiser's own follow-up is open, not
    answered — unless a non-raiser reply precedes it: confirmation and
    complaint look identical to code, so that ambiguity goes to the judge.
    """
    if t.resolved or t.outdated or len(t.comments) < 2:
        return False
    if t.last_author != t.first_author:
        return not _earlier(t.comments[-1].created_at, t.comments[0].created_at)
    return (
        any(c.author != t.first_author for c in t.comments[1:])
        and not _earlier(t.comments[-1].created_at, t.comments[0].created_at)
    )


def _earlier(later: str, first: str) -> bool:
    """True when *later* is strictly before *first* as a moment in time.

    GitHub returns ISO 8601, but not always in one shape: comparing the strings
    put `2026-01-01T09:30:00Z` before `2026-01-01T10:00:00+02:00` although it
    happens 90 minutes after it. Unparseable values fall back to the string
    comparison rather than raising.
    """
    a, b = _moment(later), _moment(first)
    if a is None or b is None:
        return str(later) < str(first)
    return a < b


def _moment(text: str) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def classify_thread(t: Thread) -> Classification:
    """Classify a thread by the ordered rules: resolved, stale, answered, else open."""
    if is_resolved(t):
        rule = "resolved"
    elif is_stale(t):
        rule = "stale"
    elif is_answered(t):
        rule = "answered"
    else:
        rule = "open"

    return Classification(thread_id=t.id, state=rule, rule=rule)


__all__ = ["is_resolved", "is_stale", "is_answered", "classify_thread"]
