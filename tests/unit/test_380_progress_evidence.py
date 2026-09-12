"""380: judge a performer from what it produced, not from the wall clock.

Two runs one card apart on the website symphony, measured 2026-09-12:

  the stuck turn      events arriving continuously (164 progress deltas),
                      zero tool_use, files_changed=0, ~2M tokens, 45 min
  a healthy run       ZERO events for 14 minutes (full suite + two model
                      calls, CPU 0.67%), then 49 tool_use in the window

A timer that resets while events arrive never fires on the first. A timer that
fires when events stop kills the second. The same field, read the same way,
gets both wrong -- so the floor has to be what was PRODUCED, not what was
EMITTED.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.services.progress_evidence import (
    production_fingerprint,
    read_evidence,
)


def _ev(kind: str, text: str = "", n: int = 1) -> list[dict]:
    return [{"type": kind, "text": text} for _ in range(n)]


# --- reading the evidence ----------------------------------------------------

def test_talking_is_not_producing():
    """The measured stuck turn: 164 progress deltas, zero tool_use."""
    ev = read_evidence(_ev("progress", "thinking about it", 164))
    assert ev.tool_uses == 0
    assert ev.total_events == 164
    assert ev.produced_anything is False


def test_commands_run_count_as_production():
    """The measured healthy run: 49 tool_use in the same size window."""
    ev = read_evidence(_ev("tool_use", "bundle exec rspec", 49) + _ev("progress", "...", 141))
    assert ev.tool_uses == 49
    assert ev.produced_anything is True


def test_a_completed_step_counts_as_production():
    ev = read_evidence(_ev("completed", "milestone 0 done"))
    assert ev.produced_anything is True


def test_thinking_and_cost_are_not_production():
    ev = read_evidence(_ev("thinking", "hmm", 40) + _ev("cost", "2,000,000 tokens total", 10))
    assert ev.produced_anything is False
    assert ev.tool_uses == 0


def test_unknown_event_types_are_not_counted_as_production():
    """Fail closed on an unrecognised type: counting it as production would
    make a stuck run look busy, which is the failure this exists to prevent."""
    ev = read_evidence(_ev("some_future_type", "?", 5))
    assert ev.produced_anything is False


def test_malformed_events_do_not_crash_the_monitor():
    """Events come off the wire. A monitor that raises on a bad one takes the
    supervisor down with it."""
    ev = read_evidence([None, "a string", {"no_type": 1}, 42, {"type": "tool_use"}])
    assert ev.tool_uses == 1
    assert ev.produced_anything is True


def test_empty_stream_produced_nothing():
    ev = read_evidence([])
    assert ev.produced_anything is False
    assert ev.total_events == 0


# --- the fingerprint: has anything NEW been produced since last poll? --------

def test_fingerprint_advances_only_on_production():
    talk = _ev("progress", "x", 10)
    assert production_fingerprint(talk) == production_fingerprint(talk + _ev("progress", "y", 30)), \
        "more talking advanced the fingerprint"
    assert production_fingerprint(talk) != production_fingerprint(talk + _ev("tool_use", "ls")), \
        "a command did not advance the fingerprint"


def test_fingerprint_is_stable_for_an_unchanged_stream():
    stream = _ev("tool_use", "a", 3) + _ev("progress", "b", 5)
    assert production_fingerprint(stream) == production_fingerprint(list(stream))


# --- the timer anchor --------------------------------------------------------

def test_the_clock_runs_from_the_last_production_not_the_dispatch():
    """The measured healthy run had produced steadily for an hour. Anchoring on
    dispatch would kill it at the ceiling despite work landing minutes ago."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=7300),      # past the 7200s ceiling
        last_production_at=now - timedelta(seconds=60),  # but it acted a minute ago
        timeout_secs=7200,
    )
    assert v.expired is False, "a working performer was killed by the dispatch clock"
    assert v.anchor == "production"
    assert round(v.elapsed_s) == 60


def test_a_performer_that_has_produced_nothing_still_expires():
    """The other direction: the stuck turn never produced, so the clock never
    reset and the ceiling still ends it."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=7300),
        last_production_at=None,
        timeout_secs=7200,
    )
    assert v.expired is True
    assert v.anchor == "dispatch"


def test_a_quiet_but_recent_run_is_not_expired():
    """The 14-minute silent window of the healthy run, under the ceiling."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 19, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=3600),
        last_production_at=now - timedelta(seconds=840),  # 14 min of silence
        timeout_secs=7200,
    )
    assert v.expired is False


def test_a_zero_timeout_disables_the_ceiling():
    """Existing behaviour: timeout_secs <= 0 means no ceiling at all."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(now=now, dispatch_at=now - timedelta(seconds=99999),
                       last_production_at=None, timeout_secs=0)
    assert v.expired is False


def test_no_dispatch_time_never_expires():
    """Nothing to measure from is not grounds to kill a live performer."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(now=now, dispatch_at=None, last_production_at=None, timeout_secs=7200)
    assert v.expired is False


# --- advancing, not merely differing -----------------------------------------

def test_nothing_produced_is_not_an_advance_over_no_prior_reading():
    """(0, 0) differs from None. Treating that as production reset the clock on
    the first poll of every run, including a stuck one."""
    from coordinare.services.progress_evidence import production_advanced

    assert production_advanced((0, 0), None) is False


def test_a_new_command_is_an_advance():
    from coordinare.services.progress_evidence import production_advanced

    assert production_advanced((1, 0), None) is True
    assert production_advanced((2, 0), (1, 0)) is True
    assert production_advanced((1, 1), (1, 0)) is True


def test_an_unchanged_reading_is_not_an_advance():
    from coordinare.services.progress_evidence import production_advanced

    assert production_advanced((3, 1), (3, 1)) is False


def test_counts_going_down_is_not_an_advance():
    """The event buffer is capped, so a long run's early tool_use entries roll
    off and the count falls. Events being forgotten is not work being done."""
    from coordinare.services.progress_evidence import production_advanced

    assert production_advanced((2, 0), (9, 0)) is False
    assert production_advanced((0, 0), (4, 2)) is False


def test_the_declared_vocabulary_actually_governs_the_reading():
    """A constant that documents a rule but governs nothing is worse than no
    constant: editing it looks like changing behaviour and changes nothing.
    Mutation N1 (adding "progress" to the producing set) passed the whole suite
    because read_evidence hardcoded its own copy of the rule inline."""
    from coordinare.services import progress_evidence as pe

    assert "progress" not in pe.PRODUCING_EVENT_TYPES
    assert pe.PRODUCING_EVENT_TYPES == pe.COMMAND_EVENT_TYPES | pe.COMPLETION_EVENT_TYPES
    # every declared producing type must actually read as production
    for kind in pe.PRODUCING_EVENT_TYPES:
        assert pe.read_evidence([{"type": kind}]).produced_anything is True, kind
    # and nothing outside it may
    for kind in ("progress", "thinking", "cost", "output", "error"):
        assert pe.read_evidence([{"type": kind}]).produced_anything is False, kind


# --- the absolute ceiling ----------------------------------------------------

def test_a_busy_loop_still_hits_an_absolute_ceiling():
    """Adversarial review, confirmed: a performer retrying the same failing
    command forever emits a REAL tool_use each time, so production advances,
    the clock resets, and a production-anchored ceiling alone never fires.

    Anchoring only on production removed the supervisor's last absolute bound.
    A stuck-but-busy performer is exactly what the ceiling exists for, so the
    dispatch-anchored bound stays as a backstop -- wider, because it must
    tolerate legitimate long work, but never absent.
    """
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=30000),        # running ~8 hours
        last_production_at=now - timedelta(seconds=5),      # "producing" every few seconds
        timeout_secs=1200,
        absolute_timeout_secs=7200,
    )
    assert v.expired is True, "a busy loop ran past the absolute ceiling unchecked"
    assert v.anchor == "absolute"


def test_the_absolute_ceiling_does_not_fire_on_legitimate_long_work():
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=3000),
        last_production_at=now - timedelta(seconds=30),
        timeout_secs=1200,
        absolute_timeout_secs=7200,
    )
    assert v.expired is False


def test_an_absent_absolute_ceiling_keeps_the_stall_ceiling():
    """Backwards compatible: callers that pass no absolute bound behave as before."""
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(now=now, dispatch_at=now - timedelta(seconds=30000),
                       last_production_at=now - timedelta(seconds=5), timeout_secs=1200)
    assert v.expired is False
    v2 = evaluate_stall(now=now, dispatch_at=now - timedelta(seconds=30000),
                        last_production_at=None, timeout_secs=1200)
    assert v2.expired is True and v2.anchor == "dispatch"


def test_a_production_stamp_older_than_the_dispatch_is_ignored():
    """It cannot belong to this run. Enforced here rather than trusting every
    caller to clear it: forgetting to is exactly how the first cut shipped a
    stage that inherited the previous stage's clock and died on its first poll.
    """
    from coordinare.services.progress_evidence import evaluate_stall

    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    v = evaluate_stall(
        now=now,
        dispatch_at=now - timedelta(seconds=70),            # this run started 70s ago
        last_production_at=now - timedelta(seconds=150),     # the PREVIOUS run's command
        timeout_secs=100,
    )
    assert v.expired is False, "a stale stamp from a previous run killed a healthy one"
    assert v.anchor == "dispatch", "the stale stamp was used as the anchor"
    assert round(v.elapsed_s) == 70
