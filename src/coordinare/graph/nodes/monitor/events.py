"""Performer event-stream merging for the monitor (435)."""

from __future__ import annotations

from typing import Any

from coordinare.graph.nodes.monitor.constants import MAX_PERFORMER_EVENTS


def _recent_event_text(events: Any) -> list[str]:
    """The last few things the performer said, for the convergence question.

    Reads defensively: events arrive from a backend coordinare does not control,
    and this runs on the path that decides whether to kill a run.
    """
    out: list[str] = []
    for event in reversed(list(events or [])[-40:]):
        if not isinstance(event, dict):
            continue
        text = event.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
        if len(out) >= 6:
            break
    return out


def merge_performer_events(
    existing: list[Any], reported: list[Any], *, cap: int = MAX_PERFORMER_EVENTS,
) -> list[Any]:
    """Merge a backend's re-reported event list into what we already hold (358).

    Backends re-report their entire accumulated event list on every poll, so
    concatenating produced a buffer full of copies: card #106 showed 40 stored
    entries that were three unique events repeated thirteen times, all carrying
    one timestamp. The Live Events panel renders this, so an operator watching a
    twelve-minute-old performer saw the same three step names over and over.

    Aligned on sequence rather than identity: find the longest suffix of what we
    hold that is also a prefix of what was just reported, and keep only the
    remainder. A set- or key-based dedup would instead drop legitimately
    repeated entries -- two identical delta chunks in a row are real, and
    collapsing them corrupts the stream.

    Correct whether the backend's list is fully cumulative (the overlap is
    everything we hold, so only genuinely new events are appended) or itself
    rolling (the overlap is partial, and nothing already dropped upstream is
    lost from our copy).
    """
    if not reported:
        return list(existing)[-cap:]
    if not existing:
        return list(reported)[-cap:]

    # Largest k where the tail of `existing` equals the head of `reported`.
    # Checked longest-first so a short accidental match cannot win over the
    # real overlap.
    for k in range(min(len(existing), len(reported)), 0, -1):
        if existing[-k:] == reported[:k]:
            return (list(existing) + list(reported[k:]))[-cap:]
    return (list(existing) + list(reported))[-cap:]

