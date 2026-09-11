"""performer_events must not accumulate duplicates (issue #358).

Backends re-report their entire accumulated event list on every poll. The old
code appended that whole list to what was already stored, so the 100-entry
buffer filled with copies. Measured live on website card #106 mid-turn:

    total events: 40
      01:16:39 progress | implementer.baseline
      01:16:39 progress | implementer.intake
      01:16:39 progress | implementer.plan
      ... the same three, 13 more times

Three unique events, forty entries, one timestamp. The Live Events panel
renders the last 12 of these, so a performer twelve minutes into a turn showed
four repetitions of the same three step names.

The merge aligns on *sequence*, not identity, which is what lets it keep
legitimately repeated entries (two identical delta chunks in a row are real).
"""
from __future__ import annotations

from coordinare.graph.nodes.monitor_performer import (
    MAX_PERFORMER_EVENTS,
    merge_performer_events,
)


def _ev(text: str, ts: str = "2026-09-11T01:16:39Z", delta: bool = False) -> dict:
    return {"timestamp": ts, "type": "progress", "text": text, "is_delta": delta}


A, B, C, D = _ev("intake"), _ev("plan"), _ev("baseline"), _ev("turn")


class TestCumulativeBackend:
    """The shape that caused the bug: the backend re-reports everything."""

    def test_repolling_the_same_batch_adds_nothing(self) -> None:
        first = merge_performer_events([], [A, B, C])
        second = merge_performer_events(first, [A, B, C])
        assert second == [A, B, C]

    def test_repolling_many_times_never_grows(self) -> None:
        """The live failure was 13 polls of an unchanged batch."""
        stored: list = []
        for _ in range(13):
            stored = merge_performer_events(stored, [A, B, C])
        assert stored == [A, B, C]
        assert len(stored) == 3

    def test_only_the_genuinely_new_tail_is_appended(self) -> None:
        stored = merge_performer_events([A, B, C], [A, B, C, D])
        assert stored == [A, B, C, D]

    def test_growth_across_successive_polls(self) -> None:
        stored: list = []
        for batch in ([A], [A, B], [A, B, C], [A, B, C], [A, B, C, D]):
            stored = merge_performer_events(stored, batch)
        assert stored == [A, B, C, D]


class TestRollingBackend:
    """If the backend's own list rolls, nothing we already hold is lost."""

    def test_partial_overlap_keeps_our_older_events(self) -> None:
        stored = merge_performer_events([A, B, C], [B, C, D])
        assert stored == [A, B, C, D]

    def test_no_overlap_appends_everything(self) -> None:
        stored = merge_performer_events([A, B], [C, D])
        assert stored == [A, B, C, D]


class TestLegitimateRepeatsSurvive:
    """Sequence alignment, not set dedup - identical deltas are real."""

    def test_repeated_identical_deltas_are_kept(self) -> None:
        d = _ev("  ", delta=True)
        stored = merge_performer_events([], [d, d, d])
        assert stored == [d, d, d], "a key-based dedup would collapse these to one"

    def test_a_repeat_arriving_later_is_kept(self) -> None:
        """The same step name recurring in a new batch is new information."""
        stored = merge_performer_events([A, B], [A, B, A])
        assert stored == [A, B, A]


class TestEdges:
    def test_empty_report_leaves_storage_untouched(self) -> None:
        assert merge_performer_events([A, B], []) == [A, B]

    def test_empty_storage_takes_the_report(self) -> None:
        assert merge_performer_events([], [A, B]) == [A, B]

    def test_both_empty(self) -> None:
        assert merge_performer_events([], []) == []

    def test_cap_is_enforced_and_keeps_the_newest(self) -> None:
        many = [_ev(f"e{i}") for i in range(MAX_PERFORMER_EVENTS + 20)]
        stored = merge_performer_events([], many)
        assert len(stored) == MAX_PERFORMER_EVENTS
        assert stored[-1] == many[-1], "truncating the tail would drop the live edge"

    def test_cap_applies_after_merging(self) -> None:
        stored = [_ev(f"old{i}") for i in range(MAX_PERFORMER_EVENTS)]
        merged = merge_performer_events(stored, [D])
        assert len(merged) == MAX_PERFORMER_EVENTS
        assert merged[-1] == D

    def test_over_cap_storage_with_an_empty_report_keeps_the_newest(self) -> None:
        """The early-return path caps too, and from the right end."""
        many = [_ev(f"e{i}") for i in range(MAX_PERFORMER_EVENTS + 5)]
        stored = merge_performer_events(many, [])
        assert len(stored) == MAX_PERFORMER_EVENTS
        assert stored[-1] == many[-1]
        assert stored[0] == many[5], "keeping the head would pin the panel to stale events"

    def test_cap_applies_on_the_overlap_path_too(self) -> None:
        """An append that overflows *through* the overlap branch must cap.

        The other cap test has no overlap, so it exercises the fallback
        return; this one forces k>0 and then overflows.
        """
        held = [_ev(f"e{i}") for i in range(MAX_PERFORMER_EVENTS)]
        reported = [held[-2], held[-1], D]
        stored = merge_performer_events(held, reported)
        assert len(stored) == MAX_PERFORMER_EVENTS
        assert stored[-1] == D
        assert stored[0] == held[1], "the oldest event should have rolled off"

    def test_longest_overlap_wins_over_a_shorter_accidental_one(self) -> None:
        """A short tail match must not beat the real alignment.

        Held [A, B, A, B]; the backend reports [A, B, A, B, C]. A k=2 match
        (tail [A, B] vs head [A, B]) would wrongly re-append [A, B, C].
        """
        stored = merge_performer_events([A, B, A, B], [A, B, A, B, C])
        assert stored == [A, B, A, B, C]
