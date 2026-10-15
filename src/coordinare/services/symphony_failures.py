"""156: the per-symphony failure bookkeeping the Symphonies table reads.

``error_count`` is the number of failed cycles in a row, and ``last_error`` is the error of the
most recent failed cycle. A successful cycle clears both, so a symphony that recovered reads as
healthy again. The lifetime total is not kept: nothing on the dashboard needs it.
"""

from __future__ import annotations

from typing import Any

from coordinare.lib.redaction import clip_session_error, redact_state_value


def record_cycle_failure(sym_state: Any, error: BaseException) -> None:
    """A failed cycle: one more failure in a row, with its redacted, clipped error."""
    sym_state.error_count = getattr(sym_state, "error_count", 0) + 1
    sym_state.last_error = clip_session_error(redact_state_value(str(error)))


def record_cycle_success(sym_state: Any) -> None:
    """A successful cycle ends the run of failures: the count and the error both clear."""
    sym_state.error_count = 0
    sym_state.last_error = None
