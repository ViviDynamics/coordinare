# Quickstart: Dashboard Activity Feed

How to run, see, and verify the feature. Assumes a working coordinare checkout with `.venv` and `.env` present.

---

## Run it

```sh
make run            # sources .env, starts the daemon + dashboard
```

Open the dashboard (default `http://localhost:8000`). The activity feed appears on the main page below the Active Performers tile.

Expected within seconds of a card being dispatched:

- entries appearing newest-first, each timestamped and attributed to a card and stage
- the feed advancing while an agent works
- a live indicator showing the stream is connected

---

## Verify the thing this feature exists for

The point is a stall signal that survives having no Slack. Verify that directly.

### 1. No notification channels at all

```yaml
# config.yaml
notifications:
  channels: []
```

Restart, dispatch a card, and let it sit. On stock thresholds you should see:

| Elapsed | Expected |
|---|---|
| ~5 min of silence | Card marked **quiet** in the feed (`quiet_threshold_seconds`, default 300) |
| 60 min in a monitoring phase | **Stuck** entry in the feed (`stuck_alerts.per_phase_thresholds`, 3600 s) |

The quiet marker is the one that matters here — it is the only signal that fires on a stock install, because the stall watchdog ships disabled and the monitoring-phase stuck threshold is an hour.

**Before this feature, all of the above produced nothing an operator could see.** The detection ran, decided the card was stuck, and handed the decision to a notification service with no channel to route it to; `dispatch()` logged `notification_unrouted` (`services/notification.py:193-206`) and returned. The signal was lost at delivery, not at detection — which is why the fix is a second destination, not a threshold change.

### 2. Faster loop while testing

Waiting five minutes per iteration is tedious:

```yaml
stuck_alerts:
  quiet_threshold_seconds: 20     # normally 300
  threshold_seconds: 60           # normally 1800
  per_phase_thresholds:
    monitoring_performer: 60      # normally 3600
```

### 3. Stall watchdog (opt-in)

```yaml
dispatcher_dedup:
  stall_timeout_seconds: 120      # 0 = disabled, the default
```

A turn that makes no forward progress for 120 s produces a **stall** entry, then follows the existing retry-or-block path — unchanged by this feature.

### 4. A wedged agent must NOT look busy

The sharpest check, and the easiest thing to get wrong. Some backends re-report their entire accumulated event list on every poll (`monitor_performer.py:3543`). Without duplicate suppression, a wedged agent would produce a *briskly scrolling* feed — reading as healthy while it is dead.

With a backend that behaves this way, confirm that a wedged turn produces **no new entries**, and that the card goes quiet on schedule.

### 5. Liveness

Stop the daemon with the dashboard open. The feed must report itself **not live** and keep its existing entries visible, marked possibly-stale. It must not simply go still — still-and-live and still-and-dead have to look different.

Restart: the indicator returns to live and backfill repopulates the feed with no manual refresh.

---

## Client-side checks

**Most of this list is now automated.** `tests/unit/test_138_client_js.py` slices the feed JS verbatim out of `_DASHBOARD_HTML` and executes it under node against a DOM stub — no browser, no npm install, no timers:

```sh
make test-js                                    # just the JS gate
.venv/bin/pytest tests/unit/test_138_client_js.py -v
```

It lives in `tests/unit/`, so `make test`, `make test-all`, `bin/build` and CI already run it. **Automated**: C1 (append vs re-render — asserted on the DOM *operation*, since the resulting HTML is identical either way), C2, C4, C6, C7, C8, C9, C10, C11, and C3's threshold logic. The gate is mutation-verified: breaking the quiet anchor, removing the `seq` dedup, or swapping the append for a full re-render each fail it.

**Still manual — run these by hand and record the outcome in the PR description.** A skipped item is a failed gate, not an untested nicety.

| Check | What only a human/browser can confirm |
|---|---|
| C1 (second half) | A screen reader announces additions politely and does **not** re-read existing rows |
| C3 / C5 | Real-time behaviour against an actually-killed daemon: not-live inside 45 s, live again on restart |
| Layout | Panel renders correctly and the scroll container does not clip the filter or liveness controls at the narrowest supported width |
| Keyboard | Filter reachable and operable by keyboard, sensible focus order |

The full reference list follows; the automated ones are kept here because they document *intent*, which the JS gate asserts but does not explain.

| # | Behaviour | How to check | Pass condition |
|---|---|---|---|
| C1 | Live append, no full re-render | Watch the feed while an agent works | New rows appear at the **top**; existing rows do not flicker, reorder, or get re-read by a screen reader |
| C2 | `seq` dedup across reconnect | Open the dashboard, stop and restart the daemon, let backfill land | No row appears twice; the feed is not upside down |
| C3 | Silence timer → not-live (SC-010) | With the dashboard open, kill the daemon and start a stopwatch | Feed reports **not live** within **45 s** — and not before ~40 s, so a single dropped keepalive cannot trip it |
| C4 | Not-live keeps its entries (FR-027) | Continue from C3 | Retained rows stay visible and are marked possibly out of date; the feed is never cleared |
| C5 | Return to live | Restart the daemon | Indicator returns to live and backfill repopulates with no manual refresh |
| C6 | Quiet marker, normal case | Let an active card fall silent past `quiet_threshold_seconds` | Card marked quiet; **exactly one** synthetic row for the episode; it is not styled or counted as progress |
| C7 | Quiet clears | Let the card emit anything | Marking clears; phase, retry budget, and outcome unchanged |
| C8 | **Quiet after restart** (the T034a gate) | With a card mid-`monitoring_performer`, restart the daemon so the feed comes back empty, and wait past the threshold | Card is marked quiet **on its `agent_dispatch_at` alone, with zero retained entries**. If it stays unmarked forever, the anchor fell back to newest-entry-only — the exact regression FR-028 exists to prevent |
| C9 | Missing-timestamp safety | A session summary with neither a retained entry nor an `agent_dispatch_at` | Card left **unmarked** — never marked quiet off a missing timestamp |
| C10 | Filter survives live updates | Filter to one card, let another card emit | The other card's new rows do **not** appear; clearing the filter restores the full retained history without a refresh |
| C11 | Empty-state text (FR-018) | Fresh daemon, before any activity | Feed states that it covers only the current daemon run |

C8 and C9 are the quiet-anchor gate (**T034a**). C1's live-region half and keyboard reachability are the accessibility gate (**T049a**).

---

## Filter to one card

Use the card filter above the feed to narrow to a single card, then clear it. With several cards in flight, this is how you read one card's history — the stages it passed through and any warning that preceded its current state.

---

## Tests

```sh
make test                                              # unit
.venv/bin/pytest tests/unit/test_138_*.py -v           # this feature's unit tests
.venv/bin/pytest tests/integration/test_138_*.py -v    # stall/stuck → feed, zero channels
make test-all                                          # whole tree — run before pushing
make lint
```

Regression check — these must pass **unmodified**, per SC-007:

```sh
.venv/bin/pytest tests/unit/test_dashboard.py -v
```

`test_sse_generator_yields_keepalive_on_timeout` asserts the exact string `": keepalive\n\n"`. The new heartbeat is emitted *after* that comment specifically so this test keeps passing. If it fails, the emission order was reversed.

---

## Configuration reference

| Setting | Default | Effect |
|---|---|---|
| `stuck_alerts.quiet_threshold_seconds` | `300` | Silence before a card is marked quiet. `0` disables. **New in 138.** |
| `stuck_alerts.threshold_seconds` | `1800` | Stuck alert threshold |
| `stuck_alerts.per_phase_thresholds` | `{monitoring_performer: 3600, monitoring_agent: 3600}` | Per-phase override |
| `stuck_alerts.cooldown_seconds` | `1800` | Minimum spacing between repeat stuck signals |
| `dispatcher_dedup.stall_timeout_seconds` | `0` | Stall watchdog. `0` disables. |

Retention (~2000 entries) and per-entry truncation (~200 chars) are fixed constants, not configuration — they are the memory ceiling, and making them tunable would let an operator remove the bound.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Feed empty after restart | Expected — history is in-memory and per-run (FR-018 says the UI states this) |
| Feed empty with cards active | Check the liveness indicator first; if not live it is a stream problem, not an activity problem |
| No quiet marker | `quiet_threshold_seconds: 0`, or the card is not in `active_sessions` |
| No quiet marker on a card restored after a restart | The dispatch-timestamp fallback is missing. Such a card has no retained entry (the feed starts empty) and produces no stage transition, so quiet must anchor on `max(newest entry, agent_dispatch_at)` — see FR-028 |
| No stuck entry | Check the phase-specific threshold — monitoring phases default to 3600 s, not 1800 |
| No stall entry | `stall_timeout_seconds` defaults to `0` (disabled) |
| Feed scrolling while an agent is wedged | Duplicate suppression is broken. Most likely `timestamp` or `seq` leaked into the dedup key — see data-model.md |
| Older entries vanishing quickly | A chatty card is consuming the shared 2000-entry cap. Known and accepted; filter to the card you care about |
