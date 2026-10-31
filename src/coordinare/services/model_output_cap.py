"""Per-call output bounds, set by the caller that knows the phase and read by the transport.

Context variables keep each bound scoped to one awaited call without threading a parameter through every
transport signature. Transports apply the lower of their configured maximum and the cap, and a transport that
can stream stops a reply whose tail has degenerated into an exact repetition when the guard is on.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

output_token_cap: ContextVar[int | None] = ContextVar('output_token_cap', default=None)
repetition_guard: ContextVar[bool] = ContextVar('repetition_guard', default=False)
# How long a call keeps retrying a provider outage (5xx, 429, dropped connection) after its first failure, beyond the
# transport's own few quick retries; 0 keeps only those.
provider_outage_seconds: ContextVar[float] = ContextVar('provider_outage_seconds', default=0.0)

# A reply whose last REPEAT_SPAN characters repeat with a period of at most MAX_PERIOD characters is degenerate:
# at least eight exact copies of the longest period, far beyond anything a JSON envelope repeats verbatim. A loop
# that only counts upward ("end-2538", "end-2539") repeats once digit runs are treated as equal, over twice the span.
REPEAT_SPAN = 2048
COUNTING_SPAN = 2 * REPEAT_SPAN
MAX_PERIOD = 256
DIGITS = re.compile(r'[0-9]+')
REPETITION_FINISH = 'repetition'


def capped(configured: int) -> int:
    cap = output_token_cap.get()
    return min(configured, cap) if cap is not None and cap > 0 else configured


def degenerate(text: str) -> bool:
    """True when the tail of ``text`` is one short span repeated, exactly or with only its numbers changing."""
    counting = DIGITS.sub('0', text[-(2 * COUNTING_SPAN):])
    return _periodic(text, REPEAT_SPAN) or _periodic(counting, COUNTING_SPAN)


def _periodic(text: str, span: int) -> bool:
    if len(text) < span + 1:
        return False
    tail = text[-span:]
    return any(tail == text[-span - period:-period] for period in range(1, min(MAX_PERIOD, len(text) - span) + 1))
