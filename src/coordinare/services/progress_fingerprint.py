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
WINDOW_EVENTS = 40
# Upper bound on the text we analyse and on the value we return.
MAX_FINGERPRINT = 480
# A string counts as a loop only if its repeating unit fits at least this many
# times. Requiring real repetitions keeps ordinary short output -- which often
# has a long "period" equal to its own length -- from being canonicalised.
MIN_REPEATS = 3


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
    # Bound the analysed text before the periodicity scan, so cost is capped.
    tail = normalised[-limit:]

    period = _smallest_period(tail)
    if 0 < period <= len(tail) // MIN_REPEATS:
        # Periodic: collapse to a rotation-invariant representative. The "~"
        # prefix keeps a loop's canonical form from colliding with a literal
        # message that happens to equal it.
        return ("~" + _minimal_rotation(tail[:period]))[:limit]
    return tail
