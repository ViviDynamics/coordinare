"""A gateway 504 must cost a retry, not a whole architect turn.

#336: two confirmed `504 Gateway Timeout` responses from the LiteLLM gateway
each terminated an in-flight `architecting` dispatch. The 08:53 one killed a
turn that had been on `architect.blueprint` for about 27 minutes. Both took the
terminal-error path, so the card was blocked and no retry counter moved: the
issue's own "neither increments a retry counter".

Coordinare already knows a 504 is transient. `handle_system_error.TRANSIENT_STATUSES`
is declared the single source of truth for upstream-HTTP classification, and it
contains 504. But that classifier only runs on the structured `upstream_http_error`
envelope (spec 067). A 504 that arrives as a bare `reason` string, which is what an
httpx.HTTPStatusError propagating out of the performer's executor produces, reached
`_is_transient_backend_error` instead, and that function knew nothing about HTTP
status codes.

Note on how the status is read: NOT by substring. #336's own withdrawn "31
occurrences over nine hours" count came from matching a bare `504`, which matched
the microsecond field of ISO timestamps. The status is parsed from httpx's message
shape, and the timestamp cases below pin that.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.handle_system_error import TRANSIENT_STATUSES
from coordinare.graph.nodes.monitor.errors import _is_transient_backend_error
from coordinare.graph.nodes.monitor_performer import monitor_performer
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

# The reason string the live incident produced: httpx's HTTPStatusError message,
# prefixed with the exception type by JobRunner's executor handler. The host is a
# placeholder on purpose: spec-145's guard forbids the real gateway hostname
# outside specs/, and nothing here depends on which host it was.
LIVE_504 = (
    "HTTPStatusError: Server error '504 Gateway Timeout' for url "
    "'https://gateway.example.com/chat/completions'"
)


@pytest.mark.parametrize("status", sorted(TRANSIENT_STATUSES))
def test_every_transient_upstream_status_is_retryable(status: int) -> None:
    """Bound to TRANSIENT_STATUSES rather than a second hand-written list, so a
    status added there cannot silently stay terminal here."""
    kind = "Client error" if status < 500 else "Server error"
    reason = f"HTTPStatusError: {kind} '{status} Some Phrase' for url 'https://gw/chat/completions'"
    assert _is_transient_backend_error(reason) is True


def test_the_live_gateway_timeout_is_retryable() -> None:
    assert _is_transient_backend_error(LIVE_504) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_client_errors_stay_terminal(status: int) -> None:
    """A 401 is not a hiccup. Retrying it burns the budget and hides the cause."""
    reason = f"HTTPStatusError: Client error '{status} Nope' for url 'https://gw/chat/completions'"
    assert _is_transient_backend_error(reason) is False


@pytest.mark.parametrize("reason", [
    "2026-09-10T08:53:04.504999Z monitor_performer.terminal_error",
    "assessor returned 504 items in its findings list",
    "run finished in 8504 ms",
    "model emitted 504",
])
def test_a_bare_504_in_prose_or_a_timestamp_is_not_a_gateway_error(reason: str) -> None:
    """The exact false positive that produced #336's withdrawn count. Matching a
    bare `504` matched timestamp microseconds; the status must be read from
    httpx's message shape, never as a loose substring."""
    assert _is_transient_backend_error(reason) is False


@pytest.mark.asyncio
async def test_a_gateway_504_on_architecting_retries_instead_of_blocking() -> None:
    """The incident end to end: the card must not be parked in Blocked, and the
    retry counter must actually move."""
    service = _Performer({"status": "error", "reason": LIVE_504})
    state = _make_state(service=service, stage="architecting",
                        sequence=["assessing", "architecting", "implementing"])

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"       # retried, not terminal
    assert result["system_error_count"] == 1       # the counter moves
    assert result["performer_stage"] == "architecting"  # stage not abandoned
    assert "504" in result["system_error_reason"]


@pytest.mark.asyncio
async def test_a_permanent_auth_failure_still_blocks() -> None:
    """The other side: an upstream 401 must keep its terminal path."""
    service = _Performer({"status": "error", "reason": (
        "HTTPStatusError: Client error '401 Unauthorized' for url "
        "'https://gateway.example.com/chat/completions'"
    )})
    state = _make_state(service=service, stage="architecting",
                        sequence=["assessing", "architecting", "implementing"])

    result = await monitor_performer(state)

    assert result["phase"] != "system_error"


def test_a_permanent_status_never_removes_retry_a_marker_already_granted() -> None:
    """This predicate may only widen what is retryable. A reason that already
    matched an infrastructure marker must keep retrying even when a 4xx is
    quoted inside it, or the fix would silently regress spec-077 behaviour."""
    reason = (
        "subprocess_exit:1 after the CLI logged "
        "\"Client error '400 Bad Request' for url 'https://gw/chat/completions'\""
    )
    assert _is_transient_backend_error(reason) is True
