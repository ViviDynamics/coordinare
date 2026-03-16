"""Contract specification for the POST /api/force-poll endpoint (016-force-poll).

This file documents the request/response contract and serves as the basis for
contract tests.  It is NOT executable production code.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
# POST /api/force-poll
#
# Signals the daemon to begin a board poll cycle immediately, interrupting
# any current idle wait.  Equivalent to firing the internal webhook_trigger
# event.

# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
# Method  : POST
# Path    : /api/force-poll
# Headers : (none required)
# Body    : (empty — no request body)

# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

# 202 Accepted — trigger was fired; cycle will start imminently
RESPONSE_202 = {
    "status_code": 202,
    "body": {"status": "accepted"},
    # Emitted when: daemon._cycle_active is False at time of request.
    # The daemon will begin a poll cycle on the next event-loop iteration.
}

# 409 Conflict — a poll cycle is already in progress; trigger not fired
RESPONSE_409 = {
    "status_code": 409,
    "body": {"status": "cycle_in_progress"},
    # Emitted when: daemon._cycle_active is True at time of request.
    # The client should re-enable the button and display a brief message.
}

# ---------------------------------------------------------------------------
# SSE state_update payload addition
# ---------------------------------------------------------------------------
# The existing /events SSE stream gains one new field in every state_update
# event.  All other fields are unchanged.

SSE_STATE_UPDATE_NEW_FIELD = {
    "cycle_active": bool,
    # True  — a poll cycle is currently executing (button must be disabled)
    # False — daemon is idle or waiting (button may be enabled)
}

# ---------------------------------------------------------------------------
# Contract invariants (verified by contract tests)
# ---------------------------------------------------------------------------

INVARIANTS = [
    # I-001: POST /api/force-poll returns 202 when daemon is idle.
    "POST /api/force-poll → 202 when daemon._cycle_active is False",

    # I-002: POST /api/force-poll returns 409 when a cycle is in progress.
    "POST /api/force-poll → 409 when daemon._cycle_active is True",

    # I-003: A 202 response causes daemon._webhook_trigger to be set.
    "202 response ↔ daemon._webhook_trigger.is_set() becomes True",

    # I-004: A 409 response does NOT alter the daemon's trigger state.
    "409 response → daemon._webhook_trigger state unchanged",

    # I-005: Every SSE state_update event includes the cycle_active boolean.
    "SSE state_update payload contains 'cycle_active' boolean field",

    # I-006: cycle_active in SSE payload matches daemon._cycle_active at emission time.
    "SSE cycle_active == daemon._cycle_active when snapshot is built",
]
