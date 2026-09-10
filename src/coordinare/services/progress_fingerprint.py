"""327: rotation-stable progress fingerprinting for the 077 stall watchdog.

The watchdog decides "did this turn make forward progress since the last poll?"
by hashing recent event text. That signal has to survive three failure modes
seen live, all of which previously read as progress forever:

1. A wedged backend re-returning its full accumulated event list unchanged
   (codex). Fingerprinting the *count* or ``repr()`` breaks here.
2. A model looping on byte-identical output, which still grows the event list
   and the token total. Fingerprinting count/tokens breaks here.
3. A model looping on a short *cycle* delivered as token deltas (website #160).
   The chunk boundaries fall at a different point in the cycle on every poll, so
   the text of the last N events is a different *rotation* of the same repeating
   string each time. Fingerprinting that text verbatim breaks here -- measured
   live, it changed on 3 of 5 consecutive polls and the 900s watchdog could
   never accumulate.

(3) is what this module adds: when the recent text is periodic, the fingerprint
collapses to a rotation-invariant canonical form of its repeating unit, so every
rotation of the same loop hashes identically. Non-periodic text -- i.e. real
work -- is returned as-is and keeps changing exactly as before.
"""
from __future__ import annotations

from collections.abc import Mapping

# How many trailing events to consider. Deltas are tiny (a few characters), so
# the old 3-event window could not span even one cycle of a short loop, let
# alone the several repetitions needed to recognise one.
WINDOW_EVENTS = 200
# Upper bound on the value we RETURN.
MAX_FINGERPRINT = 480
#: Upper bound on the text we ANALYSE. Deliberately much larger than the
#: returned value: a repeating cycle is only visible across several
#: repetitions, and deltas are a handful of characters each. Measuring novelty
#: over a 480-char slice of the live loop scored 0.39 and missed it, while the
#: same stream over its full window scored 0.03.
ANALYSIS_CHARS = 6000
# A string counts as a loop only if its repeating unit fits at least this many
# times. Requiring real repetitions keeps ordinary short output -- which often
# has a long "period" equal to its own length -- from being canonicalised.
MIN_REPEATS = 3
#: 329: event types that mean the performer actually DID something. Tool
#: activity is the one progress signal a rambling model cannot fake -- the live
#: loop on website #160 froze its tool calls at 01:29:24 and then streamed text
#: for another ~8 minutes.
TOOL_EVENT_TYPES = frozenset({"tool_use", "tool_call", "local_shell_call"})
#: 329: a loop that PARAPHRASES itself has no exact repeating unit, so the
#: period test above cannot see it. Novelty catches it: the share of word
#: n-grams in the recent window that are distinct. Measured on captured live
#: streams -- degenerate loops scored 0.017-0.051, while genuine output
#: (rspec names, migration logs, npm lines, prose planning) scored 0.554-1.0.
#: 0.15 sits ~3x above the worst loop and ~3.6x below the least novel real
#: output.
NOVELTY_GRAM = 5
NOVELTY_FLOOR = 0.15
#: Below this many words there is not enough signal to judge novelty, so the
#: window is left alone rather than guessed at.
NOVELTY_MIN_WORDS = 60
#: Returned in place of the text when the window is a paraphrasing loop. It is
#: deliberately CONSTANT: two consecutive polls of a loop must produce equal
#: fingerprints, and the loop's own content is not stable enough to hash (the
#: distinct n-gram set drifts as the window slides).
LOOP_MARKER = "~repetition-loop"


def _smallest_period(text: str) -> int:
    """Length of the shortest ``u`` such that ``text`` is a substring of ``u``
    repeated forever (KMP failure function; O(n)).

    Returns ``len(text)`` when the text is not periodic. Because this is the
    border-based period, it also recognises a text that starts and ends
    mid-cycle -- which is exactly what a delta window looks like.
    """
    n = len(text)
    if n == 0:
        return 0
    failure = [0] * n
    k = 0
    for i in range(1, n):
        while k and text[i] != text[k]:
            k = failure[k - 1]
        if text[i] == text[k]:
            k += 1
        failure[i] = k
    return n - failure[n - 1]


def _minimal_rotation(unit: str) -> str:
    """The lexicographically smallest rotation of ``unit``.

    This is the canonicalisation step: ``"Go. Tool. No. "`` and
    ``"No. Go. Tool. "`` are the same loop observed at different offsets, and
    both reduce to the same representative.
    """
    if len(unit) < 2:
        return unit
    return min(unit[i:] + unit[:i] for i in range(len(unit)))


def _tool_signature(events: list[object]) -> str:
    """Identity of the most recent tool activity.

    Changes whenever the performer runs another command or edit, which is
    genuine forward progress no matter what the prose is doing. Stays constant
    while a model only talks -- including when tool events have aged out of the
    backend's capped event list entirely, which is what a long loop looks like.
    """
    latest = None
    for event in events:
        if isinstance(event, Mapping) and event.get("type") in TOOL_EVENT_TYPES:
            latest = event
    if latest is None:
        return "t0"
    # Identity of the NEWEST tool event only -- deliberately not a count. The
    # backend returns a capped, sliding window, so a count falls 5,4,3,2,1,0 as
    # old tool events age out, and every one of those decrements would read as
    # fresh progress and reset the stall timer. The newest event is the LAST to
    # age out, so a wedged turn changes this at most once.
    return "t:" + str(latest.get("text") or "")[:80]


def _text_fingerprint(tail: str, limit: int) -> str:
    """Canonical form of the recent text, collapsing both kinds of loop.

    Novelty is tested BEFORE periodicity. A paraphrasing loop is often *partly*
    periodic, and which period the border test happens to find shifts as the
    window slides -- so letting the period branch win first produced a
    different canonical form on each poll and defeated the whole point. The
    novelty verdict is a constant, so it cannot drift.
    """
    words = tail.split()
    if len(words) >= NOVELTY_MIN_WORDS:
        grams = [
            tuple(words[i : i + NOVELTY_GRAM])
            for i in range(len(words) - NOVELTY_GRAM + 1)
        ]
        if grams and (len(set(grams)) / len(grams)) < NOVELTY_FLOOR:
            # Almost nothing in this window is new: a loop, however it words
            # itself. Deliberately content-free, so successive polls agree.
            return LOOP_MARKER
    period = _smallest_period(tail)
    if 0 < period <= len(tail) // MIN_REPEATS:
        # Exactly periodic: collapse to a rotation-invariant representative.
        # Reached for short cycles that carry too few words to judge novelty.
        return ("~" + _minimal_rotation(tail[:period]))[:limit]
    # The TAIL, not the head: new work arrives at the end, and slicing from the
    # front would leave the fingerprint unchanged while the agent kept working.
    return tail[-limit:]


def progress_fingerprint(
    events: object,
    *,
    window: int = WINDOW_EVENTS,
    limit: int = MAX_FINGERPRINT,
) -> str:
    """A fingerprint of recent event text that is stable under loop rotation.

    Equal fingerprints across two polls mean "no forward progress". Callers must
    not read anything else into the value; it is an opaque comparison token.
    """
    if not isinstance(events, list):
        return ""
    texts: list[str] = []
    for event in events[-window:]:
        if isinstance(event, Mapping):
            value = event.get("text", "")
            if value:
                texts.append(str(value))
    if not texts:
        return ""

    # Concatenate with NO separator. Delta events are contiguous fragments of
    # one message, so any separator lands at the ragged chunk boundaries and
    # destroys the very periodicity we need to detect. The fingerprint is an
    # opaque comparison token, so running discrete messages together is fine:
    # identical concatenated text means no new content either way.
    #
    # Normalise whitespace so re-flowed but identical content is not mistaken
    # for new work, and so a cycle broken across a newline still reads as one.
    normalised = " ".join("".join(texts).split())
    if not normalised:
        return ""
    # Bound the analysed text so cost is capped, but analyse far more than we
    # return -- see ANALYSIS_CHARS.
    tail = normalised[-ANALYSIS_CHARS:]

    # Tool activity first: it is the signal a looping model cannot produce.
    tool_part = _tool_signature(events)[:120]
    text_part = _text_fingerprint(tail, max(1, limit - len(tool_part) - 1))
    return f"{tool_part}|{text_part}"
