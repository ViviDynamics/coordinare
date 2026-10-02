"""509: the silent-exit reason must classify as a transient backend error.

A claude CLI that exits 0 without producing events never ran the agent. The
monitor's retry gate (``_phase_error_status_s2``) bounces the session only
when the reason matches ``_is_transient_backend_error`` — an unprefixed
reason would park the card in Blocked instead of re-dispatching.
"""

from __future__ import annotations

from coordinare.graph.nodes.monitor.errors import _is_transient_backend_error


def test_silent_exit_reason_is_transient() -> None:
    assert _is_transient_backend_error(
        "subprocess_exit: claude exited with code 0 without producing any events",
    )


def test_silent_exit_reason_with_stderr_tail_is_transient() -> None:
    assert _is_transient_backend_error(
        "subprocess_exit: claude exited with code 0 without producing any events"
        ": some stderr tail",
    )
