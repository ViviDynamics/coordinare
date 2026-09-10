"""327: progress fingerprinting must not read a repetition loop as progress.

Live incident (website #160, 2026-09-09): a codex-ephemeral architect streamed a
degenerate ``Go. / Tool. / No.`` cycle for ~50 minutes -- zero tool calls, zero
commits -- while ``stall_timeout_seconds: 900`` was configured and enabled. The
watchdog never tripped because it fingerprinted the text of the last three
events: the backend streams token *deltas*, so chunk boundaries land at a
different point in the repeating cycle on every poll, and the fingerprint kept
changing. Measured live, it changed on 3 of 5 consecutive polls.
"""
from __future__ import annotations

from coordinare.services.progress_fingerprint import progress_fingerprint

# The unit the model actually looped on, verbatim from the live rollout.
CYCLE = "Go.\nTool.\nNo.\n"


def _delta_stream(unit: str, *, chunk_sizes: tuple[int, ...], total: int) -> list[dict]:
    """Chunk an infinite repetition of ``unit`` into ragged delta events.

    Mirrors the live shape: every event is a token delta, and the chunk
    boundaries do NOT align to the cycle, so successive windows over the stream
    are different rotations of the same periodic text.
    """
    stream = (unit * ((total // len(unit)) + 2))[:total]
    events: list[dict] = []
    pos = 0
    i = 0
    while pos < len(stream):
        size = chunk_sizes[i % len(chunk_sizes)]
        events.append({"type": "progress", "is_delta": True, "text": stream[pos : pos + size]})
        pos += size
        i += 1
    return events


def test_rotating_delta_cycle_is_stable_across_polls() -> None:
    """THE regression: successive polls over a looping delta stream must
    produce the SAME fingerprint, so ``last_progress_at`` stops being reset."""
    events = _delta_stream(CYCLE, chunk_sizes=(11, 13, 4, 9, 6), total=4000)
    # Five successive polls, each seeing the stream a bit further along --
    # exactly what the live 37s poll cadence produced.
    windows = [events[: 300 + k * 17] for k in range(5)]
    fingerprints = {progress_fingerprint(w) for w in windows}
    assert len(fingerprints) == 1, f"fingerprint rotated: {fingerprints}"


def test_identical_repeated_output_still_stable() -> None:
    """Preserve the existing defence against the 2h byte-identical hang."""
    done = {"type": "progress", "text": "## Task Complete: implemented the thing"}
    assert progress_fingerprint([done] * 60) == progress_fingerprint([done] * 89)


def test_genuine_progress_changes_the_fingerprint() -> None:
    """Real work must NOT be flattened into a stall."""
    base = [{"text": f"step {k}"} for k in range(40)]
    later = [*base, {"text": "ran pytest: 42 passed"}]
    assert progress_fingerprint(base) != progress_fingerprint(later)


def test_distinct_non_periodic_tails_are_distinct() -> None:
    """Two different real tails must not collide onto one canonical form."""
    a = [{"text": "applying migration add_project_assignments"}]
    b = [{"text": "applying migration drop_legacy_join_tables"}]
    assert progress_fingerprint(a) != progress_fingerprint(b)


def test_short_almost_periodic_text_is_not_treated_as_a_cycle() -> None:
    """`abcabd` shares a prefix with a cycle but is not one -- it must stay
    distinguishable from the true cycle `abcabc`, or real output collapses."""
    assert progress_fingerprint([{"text": "abcabd"}]) != progress_fingerprint(
        [{"text": "abcabc"}]
    )


def test_rotation_of_a_real_message_is_not_collapsed() -> None:
    """Only genuinely periodic text is canonicalised. A long non-repeating
    message must keep its own identity even if it shares a suffix."""
    a = [{"text": "creating role root then loading the test schema"}]
    b = [{"text": "loading the test schema then creating role root"}]
    assert progress_fingerprint(a) != progress_fingerprint(b)


def test_empty_and_textless_events_are_safe() -> None:
    assert progress_fingerprint([]) == ""
    assert progress_fingerprint([{"no_text": 1}, {}]) == ""
    assert progress_fingerprint("not a list") == ""  # type: ignore[arg-type]


def test_whitespace_only_variation_is_not_progress() -> None:
    """Re-flowed whitespace around identical content is not new work."""
    a = [{"text": "waiting  for\tthe   database"}]
    b = [{"text": "waiting for the database"}]
    assert progress_fingerprint(a) == progress_fingerprint(b)


def test_fingerprint_is_bounded() -> None:
    """Cost guard: the fingerprint must stay small regardless of input."""
    events = [{"text": "x" * 5000} for _ in range(200)]
    assert len(progress_fingerprint(events)) <= 480


def test_exact_rotations_of_a_non_looping_message_stay_distinct() -> None:
    """Canonicalisation must apply ONLY to text that actually repeats.

    These two are exact rotations of one another but neither is a loop. If the
    "must repeat at least MIN_REPEATS times" guard were dropped, both would
    collapse to the same representative and two genuinely different states
    would read as "no progress".
    """
    # No whitespace: normalisation must not quietly break the rotation
    # relationship and let this test pass for the wrong reason.
    first = "loadingschemathencreatingrole"
    second = first[13:] + first[:13]
    assert second != first
    assert second in {first[i:] + first[:i] for i in range(len(first))}  # a rotation
    assert progress_fingerprint([{"text": first}]) != progress_fingerprint(
        [{"text": second}]
    )


def test_a_loop_does_not_collide_with_a_literal_message_of_its_unit() -> None:
    """A model looping on "abc" must not be confused with one that emitted the
    single word "abc" and then genuinely stopped -- the canonical form is
    marked so it cannot collide with literal text."""
    looping = progress_fingerprint([{"text": "abc" * 40}])
    literal = progress_fingerprint([{"text": "abc"}])
    assert looping != literal


def test_two_repeats_is_not_yet_a_loop() -> None:
    """Boundary below MIN_REPEATS. Found in review: mutating MIN_REPEATS from 3
    to 2 left every test passing, so the threshold was unprotected. Text that
    merely repeats twice is ordinary output and must not be canonicalised."""
    assert progress_fingerprint([{"text": "ab" * 2}]) == "abab"
    assert not progress_fingerprint([{"text": "ab" * 2}]).startswith("~")
    # Three repeats is a loop and must be canonicalised.
    assert progress_fingerprint([{"text": "ab" * 3}]).startswith("~")
