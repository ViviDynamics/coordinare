"""Closer gate (spec 172 FR-005, FR-006, FR-013): pure rules over the judgements.

A judgement is accepted only for a thread that was sent, and an `addressed`
only when its quote is found in that thread's own comments. Everything else
leaves the thread open. The verdict is derived from what remains open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from performer.workflows.closer.models import Classification, Judgement, Thread

__all__ = ["quote_found", "accept_judgement", "verdict", "to_resolve", "GateOutcome", "run_gate"]


def _fold(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def quote_found(quote: str, thread: Thread) -> bool:
    """The quote, whitespace folded, appears in one of the thread's comments."""
    needle = _fold(quote)
    if not needle:
        return False
    return any(needle in _fold(c.body) for c in thread.comments)


def accept_judgement(j: Judgement, sent_ids: Iterable[str], threads_by_id: dict[str, Thread]) -> tuple[bool, str | None]:
    """(accepted, discard reason). A judgement about a thread nobody sent is discarded,
    and an addressed judgement must be traceable to the thread's own words."""
    if j.thread_id not in set(sent_ids):
        return False, "thread_not_sent"
    if not j.addressed:
        return True, None
    thread = threads_by_id.get(j.thread_id)
    if thread is None or not quote_found(j.quote, thread):
        return False, "quote_not_found"
    return True, None


def verdict(open_threads: list[Any]) -> str:
    return "changes_requested" if open_threads else "approved"


def to_resolve(classifications: list[Classification], judgements: list[Judgement]) -> list[tuple[str, str]]:
    """(thread id, reason) for every thread that earned resolution: stale by rule,
    or judged addressed and accepted."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tid, reason in (
        [(c.thread_id, "outdated: the code the thread pointed at has changed") for c in classifications if c.state == "stale"]
        + [(j.thread_id, f"addressed: {j.quote[:200]}") for j in judgements if j.accepted and j.addressed]
    ):
        if tid in seen:  # one entry per thread: a thread is resolved once, for one reason
            continue
        seen.add(tid)
        out.append((tid, reason))
    return out


@dataclass
class GateOutcome:
    judgements: list[Judgement]
    open_thread_ids: list[str]
    resolve: list[tuple[str, str]]
    verdict: str = field(default="approved")


def run_gate(classifications: list[Classification], raw_judgements: Iterable[Any], sent_ids: list[str], threads_by_id: dict[str, Thread]) -> GateOutcome:
    judgements: list[Judgement] = []
    seen: set[str] = set()
    for raw in raw_judgements:
        j = Judgement(thread_id=str(getattr(raw, "thread_id", "")), addressed=bool(getattr(raw, "addressed", False)),
                      quote=str(getattr(raw, "quote", "") or "")[:300], reason=str(getattr(raw, "reason", "") or "")[:300], accepted=False)
        if j.thread_id in seen:
            j.discard_reason = "duplicate_judgement"
            judgements.append(j)
            continue
        seen.add(j.thread_id)
        accepted, reason = accept_judgement(j, sent_ids, threads_by_id)
        j.accepted, j.discard_reason = accepted, reason
        judgements.append(j)
    closed = {j.thread_id for j in judgements if j.accepted and j.addressed}
    open_ids = [c.thread_id for c in classifications if c.state == "open"]
    open_ids += [c.thread_id for c in classifications if c.state == "answered" and c.thread_id not in closed]
    resolve = to_resolve(classifications, judgements)
    return GateOutcome(judgements=judgements, open_thread_ids=open_ids, resolve=resolve, verdict=verdict(open_ids))
