"""135 review: the re-send schedule, dedup against the budget, and code-point lengths.

Each test pins one finding of the independent review of PR #212. The re-send schedule is
driven by an injected clock, so the bounds are exact and the tests take no real time.
"""
from __future__ import annotations

import math
from typing import Any

from coordinare.services.activity_log import MAX_STREAM_ENTRIES, ActivityLog

_CARD = "PVTI_135R"
_SESSION = "session-135r"
_PERFORMER = "performer-135r"
_STREAM = "st-135r"


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _delta(log: ActivityLog, text: str, stream: str = _STREAM) -> None:
    log.record(
        activity_type="progress", card_id=_CARD, stage="implementing", text=text,
        session_id=_SESSION, performer_id=_PERFORMER, is_delta=True, stream_id=stream,
    )


def _logged_with(clock: _Clock) -> ActivityLog:
    """An activity log that reads its re-send schedule from the given clock."""
    log = ActivityLog()
    log._clock = clock
    return log


def _capture(log: ActivityLog) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    log.sink = lambda entries: sent.extend(e.to_dict() for e in entries)
    return sent


def _hidden_sent(sent: list[dict[str, Any]]) -> list[int]:
    return [m["hidden_updates"] for m in sent if m["activity_type"] == "stream_truncated"]


def test_post_cap_re_sends_are_bounded_and_the_final_count_reaches_the_sink() -> None:
    """10,000 updates dropped inside one second: at most floor(log2 N) + 2 messages, all exact."""
    dropped = 10_000
    log = _logged_with(_Clock())
    sent = _capture(log)
    for i in range(MAX_STREAM_ENTRIES + dropped):
        _delta(log, f"w{i} ")
    bound = math.floor(math.log2(dropped)) + 2
    # Before the flush: the marker, then one message per power of two (1, 2, 4, ... 8192).
    assert _hidden_sent(sent) == [2**k for k in range(14)]
    assert len(_hidden_sent(sent)) <= bound
    # The keepalive flush sends the count the stream stopped at, once.
    log.flush_pending_markers()
    assert _hidden_sent(sent)[-1] == dropped
    assert len(_hidden_sent(sent)) <= bound
    # The stored count is exact all along, not only the sent one.
    marker = [e for e in log.snapshot() if e["activity_type"] == "stream_truncated"]
    assert marker[0]["hidden_updates"] == dropped


def test_a_re_send_waits_a_second_unless_the_count_is_a_power_of_two() -> None:
    clock = _Clock()
    log = _logged_with(clock)
    sent = _capture(log)
    for i in range(MAX_STREAM_ENTRIES + 3):  # hidden 1 (the marker), 2, 3, all at t=0
        _delta(log, f"w{i} ")
    assert _hidden_sent(sent) == [1, 2]  # 3 is not a power of two, and no second has passed
    clock.now = 1.0
    _delta(log, "w-4 ")  # hidden 4: a power of two, sent at once
    _delta(log, "w-5 ")  # hidden 5: a second has not passed since the send at 1.0
    assert _hidden_sent(sent) == [1, 2, 4]
    clock.now = 2.0
    _delta(log, "w-6 ")  # hidden 6: a second has now passed since the send at 1.0
    assert _hidden_sent(sent) == [1, 2, 4, 6]
    # The send at 6 carried the current count, so nothing is left pending for the flush.
    log.flush_pending_markers()
    assert _hidden_sent(sent) == [1, 2, 4, 6]


def test_updates_that_repeat_a_kept_update_do_not_count_against_the_budget() -> None:
    """Duplicates are suppressed, so the budget and the "N of M" line count only distinct updates."""
    texts: list[str] = []
    for k in range(MAX_STREAM_ENTRIES + 1):
        texts.append(f"w{k} ")
        texts.append("\n")  # a token that repeats: every copy after the first is a duplicate
    distinct = len(set(texts))
    log = ActivityLog()
    for text in texts:
        _delta(log, text)
    entries = log.snapshot()
    shown = [e for e in entries if e["is_delta"]]
    marker = [e for e in entries if e["activity_type"] == "stream_truncated"]
    assert len(shown) == MAX_STREAM_ENTRIES
    assert len(marker) == 1
    assert len(shown) + marker[0]["hidden_updates"] == distinct


def test_half_duplicates_under_the_cap_show_no_marker_and_no_count() -> None:
    log = ActivityLog()
    for i in range(210):
        _delta(log, "\n" if i % 2 == 0 else f"w{i} ")
    entries = log.snapshot()
    assert not [e for e in entries if e["activity_type"] == "stream_truncated"]
    assert len([e for e in entries if e["is_delta"]]) == 106  # 105 distinct words and one newline


def test_text_length_counts_code_points_not_utf16_units() -> None:
    log = ActivityLog()
    emoji = log.record(activity_type="progress", card_id=_CARD, text="\U0001F600" * 210)
    assert emoji is not None
    assert emoji.text_length == 210
    assert len(emoji.text) == 200
    mixed = log.record(activity_type="progress", card_id=_CARD, text="a\U0001F600b" * 100)
    assert mixed is not None
    assert mixed.text_length == 300
    assert len(mixed.text) == 200
