# Quickstart: Live Web Dashboard (010)

## Configuration

Add two optional fields to `config.yaml` (both have defaults):

```yaml
dashboard_port: 8090        # default: 8090 (separate from health_check_port: 8080)
dashboard_host: "127.0.0.1" # default: localhost-only; set "0.0.0.0" for reverse proxy
```

Or via environment variables (no config.yaml change needed):

```bash
export COORDINARE_DASHBOARD_PORT=8090
export COORDINARE_DASHBOARD_HOST=127.0.0.1
```

## Starting the Daemon

No change to the startup command — the dashboard starts automatically:

```bash
cd src && python -m coordinare
```

On startup you will see:

```
dashboard_started  host=127.0.0.1  port=8090
```

If port 8090 is already in use:

```
dashboard_port_conflict  port=8090  error="[Errno 48] Address already in use"
```

The daemon exits immediately with code 1.

## Opening the Dashboard

Navigate to `http://localhost:8090/` in any modern browser. No login required.

What you will see on first load:
- **Phase**: Current workflow phase (e.g., "Idle", "Monitoring Agent")
- **Active Card**: Card title, board column, PR link (if applicable) — or "No active card" when idle
- **Agent Session**: Session ID if an agent session is active
- **Open Questions**: Blocked-phase questions (only when daemon is in `blocked` phase)
- **Subsystem Health**: Health indicator for `github`, `agent_ssh`, `config`, `notifications`
- **Metrics**: Cycles completed, last cycle duration, error count, daemon start time
- **Cycle History**: Up to 20 most recent cycles (empty-state message if no cycles yet)

## Live Updates

The page auto-updates within 5 seconds of any state change — no manual refresh needed.

To watch a phase transition live:
1. Open the dashboard
2. Trigger a board change (move a card to "In Progress")
3. Wait one poll interval (default 30 seconds)
4. The dashboard phase updates to "Dispatching" → "Monitoring Agent" automatically

## Disconnected State

If the daemon is stopped while the dashboard is open:
- A **"Disconnected"** banner appears over the last-known state within a few seconds
- When the daemon restarts (within 10 seconds per SC-005), the banner clears and live data resumes automatically

## Reverse Proxy Setup

To expose the dashboard on a network (e.g., via nginx):

```yaml
dashboard_host: "0.0.0.0"
dashboard_port: 8090
```

Then proxy from nginx:

```nginx
location /coordinare/ {
    proxy_pass http://localhost:8090/;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding on;
}
```

The `Connection: ''` and `proxy_http_version 1.1` lines are required for SSE to work through nginx without buffering.

## E2E Walkthrough Scenarios

### Scenario 1: Idle daemon

**Setup**: Daemon running, no cards in "In Progress" column.

**Expected**:
- Phase: "Idle"
- Active Card section: "No active card"
- Cycle History: shows count of completed cycles, or empty-state message if fresh start
- Subsystems: all "healthy" (or "unavailable" for notifications if unconfigured)

### Scenario 2: Active card in monitoring phase

**Setup**: Move a card to "In Progress" on the board. Wait one poll cycle.

**Expected within 5 seconds of cycle completion**:
- Phase: "Monitoring Agent"
- Active Card: card title, "In Progress" column, PR link (clickable) if PR exists
- Agent Session: session ID shown if agent dispatched
- Last Cycle Duration: updated

### Scenario 3: Blocked phase with open questions

**Setup**: Card transitions to `blocked` phase (agent has open questions).

**Expected**:
- Phase: "Blocked"
- Active Card: card details still shown
- Open Questions section: list of questions from the agent

### Scenario 4: Subsystem degraded

**Setup**: Simulate GitHub circuit breaker open (or disconnect network).

**Expected**:
- `github` subsystem shows "degraded" (visually distinct from healthy)
- `required: true` indicator shown
- Other subsystems unaffected

### Scenario 5: Cycle history accumulation

**Setup**: Run daemon through 5+ cycles (mix of success and error-recovery cycles).

**Expected**:
- History section shows up to 20 entries, newest at top
- Error cycles visually distinct from successful cycles
- Timestamps and durations accurate

### Scenario 6: Port conflict startup failure

**Setup**: Start a second coordinare process with the same dashboard port.

**Expected**:
- Structured log: `dashboard_port_conflict  port=8090`
- Daemon exits with code 1 immediately
- First daemon continues running unaffected

### Scenario 7: Browser reconnect after daemon restart

**Setup**: Dashboard open in browser. Stop daemon (`Ctrl+C`). Restart daemon.

**Expected**:
1. "Disconnected" banner appears within a few seconds of daemon stop
2. After daemon restarts, banner clears within 10 seconds (SC-005)
3. Fresh data displayed (history resets to empty since daemon restarted)
4. Daemon start time updates to new timestamp

## Testing the SSE Stream Directly

```bash
curl -N http://localhost:8090/events
```

You will see:

```
event: state_update
data: {"phase": "idle", "phase_label": "Idle", ...}

: keepalive

: keepalive

event: state_update
data: {"phase": "dispatching", "phase_label": "Dispatching", ...}
```

Press `Ctrl+C` to disconnect.
