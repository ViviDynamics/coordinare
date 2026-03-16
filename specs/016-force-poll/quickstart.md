# Quickstart: Dashboard "Check Board Now" Button (016-force-poll)

## Integration Scenarios

### Scenario 1: Operator triggers a poll while daemon is idle

```
Preconditions:
  - Coordinare daemon is running
  - Dashboard is open in a browser
  - Daemon is in idle state (cycle_active=False in SSE stream)

Steps:
  1. Operator observes "Idle" phase indicator on dashboard
  2. "Check Board Now" button is visible and enabled
  3. Operator clicks the button
  4. Button immediately becomes disabled and shows a spinner
  5. Client POSTs to /api/force-poll
  6. Server responds 202 Accepted
  7. Daemon receives the trigger and starts a poll cycle
  8. Next SSE state_update arrives with cycle_active=True
  9. Button remains disabled (cycle in progress)
  10. Poll cycle completes; SSE delivers cycle_active=False
  11. Button re-enables and spinner clears

Expected outcome: One additional poll cycle executes immediately.
```

---

### Scenario 2: Operator clicks button while cycle is already running

```
Preconditions:
  - Daemon is currently executing a poll cycle (cycle_active=True)
  - Either the SSE has already updated the button to disabled, OR
    the operator clicked just before the SSE arrived

Steps:
  1. Operator clicks "Check Board Now"
  2. Button disables and shows spinner
  3. Client POSTs to /api/force-poll
  4. Server responds 409 Conflict (cycle already in progress)
  5. Client displays brief inline message: "Cycle already running"
  6. Button re-enables (or remains disabled if SSE still shows cycle_active=True)

Expected outcome: No additional cycle is triggered. Daemon is not disturbed.
```

---

### Scenario 3: Trigger in webhook-only mode (poll_interval=0)

```
Preconditions:
  - Coordinare configured with poll_interval_seconds=0
  - Daemon is waiting on webhook_trigger or stop_event

Steps:
  1. Same as Scenario 1 — button click POSTs to /api/force-poll
  2. Server sets webhook_trigger event
  3. Daemon's asyncio.wait() unblocks immediately
  4. Poll cycle executes

Expected outcome: Button works identically regardless of poll interval.
```

---

### Scenario 4: Server unreachable

```
Preconditions:
  - Dashboard is open but coordinare server has restarted or is temporarily unreachable

Steps:
  1. Operator clicks "Check Board Now"
  2. fetch() call throws a network error or receives a 5xx
  3. Client shows brief error message: "Could not reach server"
  4. Button re-enables after the error so operator can retry

Expected outcome: Operator is informed of the failure and can retry.
```

---

## Testing the Feature Manually

```bash
# 1. Start coordinare with a config
coordinare --config config.yaml

# 2. Open dashboard in browser (default http://127.0.0.1:8090)

# 3. Confirm button is present and enabled when idle

# 4. Trigger via curl (same as clicking):
curl -X POST http://127.0.0.1:8090/api/force-poll
# Expected: {"status":"accepted"} with HTTP 202

# 5. Trigger again immediately (cycle in progress):
curl -X POST http://127.0.0.1:8090/api/force-poll
# Expected: {"status":"cycle_in_progress"} with HTTP 409

# 6. Verify SSE stream includes cycle_active:
curl -N http://127.0.0.1:8090/events
# Look for: "cycle_active":true / "cycle_active":false in data lines
```
