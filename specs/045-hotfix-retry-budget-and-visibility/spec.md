# 045 — Hotfix: Retry Budget & Backend Visibility

## Summary

Three hotfixes surfaced during live testing immediately after spec 044 shipped:
the retry budget is dead code (transport errors swallowed before counting),
non-JSON performer output is invisible at info level, and session_expired
doesn't move the card to IN_REVIEW causing an IN_PROGRESS loop.

## Motivation

Spec 044 added a relay retry budget that blocks a card after 3 consecutive
transport errors. Live testing on 2026-04-15 showed it never fires: 7
consecutive `check_status_transport_error` warnings produced
`system_error_count=0` at session expiry. Root cause: `agent_service.py`
catches `TransportError` and returns `{"status": "unknown"}` instead of
re-raising — `monitor_performer` never reaches its error-counting except
block.

## Changes

### 1. Re-raise TransportError from agent_service.check_status (O-8)

`agent_service.py:68-70` was swallowing `TransportError` and returning a
dict with `status=unknown`. The "unknown" status falls through all marker
checks in `monitor_performer` and is treated as "still working" — no
error count, no escalation.

Fix: remove the try/except in `check_status` so `TransportError` propagates
to `monitor_performer`'s transport error handler (line 488-516), which
correctly increments `system_error_count` and routes to `system_error`.

### 2. Log non-JSON performer output at warning level (O-9)

`subprocess_transport.py` logs skipped non-JSON lines at debug level.
With the default info log level, operators see "No valid JSON response
in output (1 lines checked)" but not what the performer actually sent.

Fix: change from debug to warning so the line preview (first 200 chars)
is visible in production logs.

### 3. Move card to IN_REVIEW on session_expired with open PR (O-6)

When session_expired fires with `pr_node_id` set, coordinare sets
`phase=monitoring_pr` but doesn't move the card on the GitHub board.
The card stays in IN_PROGRESS, and `check_board` re-routes to
`monitoring_agent` on the next cycle — infinite loop.

Fix: call `github.move_card(card_id, "IN_REVIEW")` in the
session_expired handler when resuming monitoring_pr.

## Success Criteria

- [ ] 7 consecutive transport errors produce system_error_count >= 3
      at session_expired, triggering the retry budget
- [ ] Non-JSON performer output is visible in info-level logs
- [ ] Session expired with open PR doesn't loop through IN_PROGRESS
