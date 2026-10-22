"""A per-call output token cap, set by the caller that knows the phase and read by the transport.

A context variable keeps the cap scoped to one awaited call without threading a parameter through every
transport signature. Transports apply the lower of their configured maximum and this cap.
"""
from __future__ import annotations

from contextvars import ContextVar

output_token_cap: ContextVar[int | None] = ContextVar('output_token_cap', default=None)


def capped(configured: int) -> int:
    cap = output_token_cap.get()
    return min(configured, cap) if cap is not None and cap > 0 else configured
