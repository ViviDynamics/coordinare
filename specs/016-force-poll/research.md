# Research: Dashboard "Check Board Now" Button (016-force-poll)

## Decision Log

### Decision 1: Trigger Mechanism

**Decision**: Fire `daemon._webhook_trigger.set()` directly from the new API endpoint.

**Rationale**: The `_webhook_trigger` asyncio.Event is already the canonical way to wake the daemon immediately — it is used by the existing webhook handler (`register_webhook_route`) and tested extensively in 015. No new daemon plumbing is needed; the button is simply an operator-facing alias for the same signal.

**Alternatives considered**:
- Add a new `daemon.force_poll()` method: Unnecessary abstraction over a one-liner; adds API surface without value.
- Signal via a separate asyncio.Queue: Overengineered for a single-consumer, edge-triggered event.

---

### Decision 2: HTTP Endpoint Design

**Decision**: `POST /api/force-poll` — returns `202 Accepted` with `{"status": "accepted"}` if the trigger was fired; returns `409 Conflict` if a cycle is already in progress (`daemon._cycle_active is True`).

**Rationale**: POST is correct for a state-changing action (triggering a cycle). 202 communicates that the work has been scheduled but not yet completed, which is accurate. 409 prevents the race condition where the operator clicks just as a cycle begins — the client gets an explicit signal rather than a silent no-op.

**Alternatives considered**:
- Always return 202, even if cycle_active: Simpler, but hides the "already running" case from the client, making it harder to give accurate button feedback.
- Return 200 with a `triggered: false` field when already running: Less semantically clear than a 409.

---

### Decision 3: Button Disabled State

**Decision**: The SSE `state_update` payload gains a new boolean field `cycle_active`. The button is disabled whenever `cycle_active=True` OR a pending trigger request is in flight (client-side flag). It re-enables on the first SSE update where `cycle_active=False`.

**Rationale**: The SSE stream already delivers phase and health data to the browser on every cycle. Adding `cycle_active` to the snapshot is a one-line change in `DashboardStore.build_snapshot()`. This avoids polling and keeps the button in sync with server state automatically.

**Alternatives considered**:
- Derive state solely from `phase` field: Phase-based logic is fragile — the daemon can be in phase "idle" at the start of a cycle, before the graph node executes. `cycle_active` is the authoritative flag.
- Add a separate `/api/daemon-status` polling endpoint for the button: Unnecessary complexity when SSE is already live.

---

### Decision 4: Client-Side Debounce / In-Flight State

**Decision**: On button click: (1) immediately disable the button and show a spinner, (2) POST to `/api/force-poll`, (3) on any response (success or error) update the button label/state, (4) re-enable only when the next SSE `cycle_active=False` arrives.

**Rationale**: Disabling immediately prevents double-clicks before the server responds. Waiting for SSE confirmation (rather than re-enabling immediately on HTTP response) avoids a flash where the button is briefly active between "trigger acknowledged" and "cycle starting".

**Alternatives considered**:
- Re-enable immediately after HTTP 202: Simpler but produces a visible flicker — the button is enabled for ~1 SSE tick before the cycle starts and `cycle_active` flips to True.
- Re-enable after a fixed timeout: Fragile; the right signal is the SSE event, not a wall-clock guess.

---

### Decision 5: No New Dependencies

**Decision**: Implement using existing FastAPI, stdlib `asyncio`, and the vanilla JavaScript already embedded in `_DASHBOARD_HTML`.

**Rationale**: The entire feature is a one-endpoint backend addition and a small JS/HTML delta in the existing inline dashboard. Adding a JS framework or a new Python package for this scope would violate the constitution's minimal-dependencies principle.
