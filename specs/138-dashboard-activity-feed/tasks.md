---
description: "Task list for 138-dashboard-activity-feed"
---

# Tasks: Dashboard Activity Feed

**Input**: Design documents from `/specs/138-dashboard-activity-feed/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/activity-event.md, quickstart.md

**Tests**: REQUIRED. The spec's acceptance criteria call for "unit/integration coverage for the new event stream and stall/stuck→feed path" (SC-008), and Constitution Principle II is non-negotiable. Test tasks are written before the implementation they cover and must fail first.

**Organization**: Grouped by user story. US1 and US2 are both P1 — US1 is the MVP, US2 is the answer to the reported bug.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable — different file, no dependency on an incomplete task
- **[Story]**: US1 / US2 / US3, mapping to spec.md user stories

## Path Conventions

Single project. Source at `src/coordinare/`, tests at `tests/unit/` and `tests/integration/`. The dashboard front end is inlined in `src/coordinare/dashboard.py`.

---

## Phase 1: Setup

**Purpose**: Establish the regression baseline and the new module's shell.

- [X] T001 Capture the SC-007 regression baseline: run `.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/dashboard/ tests/unit/test_dashboard_config_api.py -v` and record that all pass before any change. These same tests must pass unmodified at the end.
- [X] T002 [P] Create `src/coordinare/services/activity_log.py` with house-style header only — `from __future__ import annotations`, `structlog.get_logger(__name__)`, and module docstring naming FR-019 through FR-024, FR-035 through FR-037, and FR-001/FR-004 (the sink fan-out) as the requirements it owns. No logic yet.

**Checkpoint**: Baseline recorded, module file exists, test authoring can begin in parallel.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The activity log, its SSE transport, and the feed panel shell. All three user stories depend on every task in this phase.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests (write first — must fail before implementation)

- [X] T003 [P] Write `tests/unit/test_138_activity_log.py` covering guarantees G1–G11 from `contracts/activity-event.md` and invariants 1–7 from `data-model.md`: retention cap, oldest-first eviction, truncation length and prefix preservation, `truncated` flag, monotonic non-reused `seq`, per-card seen-set bound, no-raise on malformed input, and the sink contract (G11 — called with exactly the appended entries, never with an empty list, and a raising sink neither propagates nor prevents the append).
- [X] T004 [P] Add to `tests/unit/test_138_activity_log.py` the three named behaviour tests: `test_wedged_agent_reemits_nothing` (re-record an unchanged batch 100×, assert zero appended — SC-009), `test_memory_ceiling_under_flood` (100k oversized records leave ≤2000 entries each ≤200 chars — SC-013), and `test_timestamp_not_in_dedup_key` (same event at two different times appends once).
- [X] T005 [P] Write `tests/unit/test_138_activity_feed_sse.py` with the seven contract tests from `contracts/activity-event.md` §1.6: **`state_update` shape additive-only**, no `_event` key in snapshots, `activity_event` emitted with entries, backfill follows the initial `state_update`, keepalive precedes heartbeat, entries oldest-first on the wire, slow client drops without blocking.

      `test_state_update_shape_additive_only` asserts every pre-138 key is still present with the same type and meaning, none removed or repurposed, and that the **only** added key is `activity_quiet_threshold_seconds` (T016). Do not write it as "keys identical to pre-138" — T016 adds a key by design, and FR-002 permits exactly that. Naming the one expected addition is what still catches an unplanned second one.

### ActivityLog implementation

- [X] T006 Implement `ActivityEntry` in `src/coordinare/services/activity_log.py`: the nine fields from `data-model.md`, immutable, with a `to_dict()` emitting `timestamp` as ISO-8601 UTC.
- [X] T007 Implement `ActivityLog.__init__` and `record()` in `src/coordinare/services/activity_log.py` with `_entries: deque[ActivityEntry]` at `maxlen=2000` and monotonic `_next_seq`. Append newest to the right; `snapshot()` returns `deque` order unreversed (oldest-first), matching the wire contract. Newest-first is applied by the client, never the server. (depends on T006)
- [X] T008 Add ingest-time truncation to `record()` in `src/coordinare/services/activity_log.py`: `text` to 200 chars and `card_title` to 80, preserving the prefix and setting `truncated=True`. Truncation MUST happen before the dedup key is built. (depends on T007)
- [X] T009 Add duplicate suppression to `record()` in `src/coordinare/services/activity_log.py`: per-card `_seen: dict[str, set[str]]` paired with `_seen_order: dict[str, deque[str]]` at `maxlen=256`, key `f"{activity_type}|{card_id}|{stage}|{text}"`. Return `None` when suppressed. **`timestamp` and `seq` must not appear in the key** — add an inline comment saying so, since this is the invariant most at risk from a later edit. (depends on T008)
- [X] T010 Implement `record_many()`, `snapshot(limit=None)` and `forget_card()` in `src/coordinare/services/activity_log.py`. `forget_card` clears bookkeeping only and never removes entries. Also add the `sink` attribute (default `None`) and fire it from **both** `record` and `record_many` with the entries actually appended — never with an empty list, and wrapped in `contextlib.suppress(Exception)` so a broken transport cannot break recording (G11). This is the live fan-out path: writers are graph nodes that hold an `ActivityLog` but no broadcaster, so without the sink every pushed entry would reach an open browser only on its next reconnect backfill. (depends on T009)

### SSE transport

- [X] T011 Add `broadcast_activity(entries)` to `SSEBroadcaster` in `src/coordinare/dashboard.py`, enqueuing `{"_event": "activity_event", "entries": [...]}` via the existing `put_nowait` + `suppress(QueueFull)` path so backpressure behaviour is unchanged (FR-004).
- [X] T012 Update `sse_stream()` in `src/coordinare/dashboard.py` to branch on `payload.get("_event")`, emitting `activity_event` for tagged payloads and `state_update` for everything else. Untagged payloads must serialise byte-identically to today. (depends on T011)
- [X] T013 Add on-connect backfill to `sse_stream()` in `src/coordinare/dashboard.py`: after the existing initial `state_update`, emit one `activity_event` with the retained history oldest-first (FR-003). The `state_update` must stay first. `snapshot()` already returns oldest-first, so pass it through unreversed; a reversal here would render the backfill upside down given T019's prepend. (depends on T012)
- [X] T014 Add the heartbeat to the idle-timeout branch of `sse_stream()` in `src/coordinare/dashboard.py`: yield the existing `": keepalive\n\n"` comment **first**, then `event: heartbeat`. Order is load-bearing — `tests/unit/test_dashboard.py:890` reads exactly one message and asserts the comment (research R4). (depends on T012)
- [X] T015 Have `DashboardStore.__init__` in `src/coordinare/dashboard.py` construct and own an `ActivityLog`, wire `self.activity_log.sink = self.broadcaster.broadcast_activity`, and cancel nothing new in `shutdown()` (no new task is introduced). The sink wiring is the **only** place the log and the transport meet — every writer (the watcher and all four graph-node push sites) fans out through it, so no call site needs to know SSE exists. (depends on T010, T011)
- [X] T016 Add `activity_quiet_threshold_seconds` to `build_snapshot()` in `src/coordinare/dashboard.py`, sourced from config with the default applied when config is absent. This is the **one** key 138 adds — FR-002 is additive-only, so add no others, and keep T005's `test_state_update_shape_additive_only` in sync with that fact. Quiet detection's other input, `agent_dispatch_at`, is already in each session summary (`dashboard.py:495-499`) — do not re-add it.

### State threading

Foundational because **every** push site needs it — agent events (US1, T023) as well as stall/stuck/recovered/error (US2). IDs keep their original numbering from when this block sat in Phase 4.

- [X] T036 [P] Add the `activity_log: ActivityLog | None` field to `CoordinareState` in `src/coordinare/graph/state.py`, beside `notification_service` at line 102. `CoordinareState` is a `TypedDict(total=False)` (`:85`), so the field is optional by construction and every reader uses `state.get("activity_log")`.
- [X] T037 Thread the `DashboardStore`-owned `ActivityLog` into the initial state in `src/coordinare/__main__.py`. `_bootstrap_services` builds the state dict at `:838-890` but never receives the store, which is constructed later at `:946` — so add an `activity_log: ActivityLog | None = None` keyword parameter to `_bootstrap_services` and pass `dashboard_store.activity_log` at the call site (`:959`), where the store already exists. Set the key beside `notification_service` at `:844`. Do **not** construct a second `ActivityLog` here — the dashboard and the graph nodes must share one instance, or pushed entries land in a log nobody serves and nothing reaches the feed. (depends on T036, T015)

### Feed panel shell

- [X] T017 Add the feed panel markup to the dashboard HTML in `src/coordinare/dashboard.py`: a container with `role="log"`, `aria-live="polite"`, `aria-relevant="additions"`, a heading, a filter slot, a liveness slot, and an empty-state element (research R5).
- [X] T018 Add feed CSS to `src/coordinare/dashboard.py` using existing `--color-*` custom properties only — no hard-coded colour values, per Constitution Principle III. Reuse `.ev-progress`, `.ev-tool_use`, `.ev-thinking`, `.ev-cost`, `.ev-error`; scroll container mirroring `.perf-log`.
- [X] T019 Add the client `activity_event` listener in `src/coordinare/dashboard.py` that **appends only the newly received entries** to the top of the feed and never re-renders the whole list — a full re-render would make `aria-relevant="additions"` re-announce every row (research R5). De-duplicate on `seq` so reconnect backfill cannot double-insert. (depends on T017)

**Checkpoint**: The log records and evicts correctly, entries reach the browser over SSE, the panel renders them, and every pre-existing dashboard test still passes. User stories can now proceed.

---

## Phase 3: User Story 1 — Tell "working" from "wedged" at a glance (Priority: P1) 🎯 MVP

**Goal**: The feed advances on its own while an agent works, stays still when nothing is happening, and never lets a dead stream masquerade as a quiet system.

**Independent Test**: Run the daemon against a card that produces agent activity; confirm entries appear without refreshing, each timestamped and attributed. Leave the system idle and confirm the feed stays still while still reporting itself live. Kill the daemon and confirm the feed reports not-live.

### Tests for User Story 1

- [X] T020 [P] [US1] Write `tests/integration/test_138_activity_feed_live.py` asserting that invoking `monitor_performer` with a performer status carrying events produces matching feed entries **in that same invocation**, each carrying timestamp, card id, card number, title, and stage (FR-005, SC-004). Drive it through the node with a stubbed performer response — **do not** assert by injecting a mutation into `daemon.state`. That shortcut is what made the original derived design look testable: it skips the only latency SC-004 measures, so it passes whether or not entries ever reach a browser in time.
- [X] T021 [P] [US1] Add to `tests/integration/test_138_activity_feed_live.py` the wedged-agent case: a backend re-reporting an unchanged accumulated event list across many polls produces zero new entries (SC-009). This is the headline behaviour — a wedged agent must never look busy. Note this is why the push site can hand the *whole* reported list to `record_many` without pre-diffing: suppression is the log's job (FR-022, FR-023).
- [X] T022 [P] [US1] Add to `tests/integration/test_138_activity_feed_live.py` a stage-transition case asserting a `stage_change` entry naming the card and the stage entered (FR-006). Assert appearance within one watcher tick of the *writeback*, not of the transition — stage changes are cycle-granular by design (SC-004a), so a tighter assertion would be testing something the design does not promise.
- [X] T025 [P] [US1] Add to `tests/integration/test_138_activity_feed_live.py` the sink fan-out assertions, on a connection that is already open and never reconnects: a watcher tick that records a stage-change batch arrives as exactly **one** `activity_event`; a single entry pushed by a graph node arrives as one `activity_event` **without** a reconnect; a tick that recorded nothing emits nothing. This is the gap the original per-call-site broadcast design left — pushed entries that only appeared on the next backfill, which would silently defeat FR-009/FR-010/FR-012 and SC-001 for an operator already watching.

      Written with the other US1 tests and failing first, like all of them — but note it can only turn green once T015's sink wiring and at least one writer (T023 or T024) exist, so it is the last US1 test to pass rather than the first.

### Implementation for User Story 1

- [X] T023 [US1] Record the agent-event batch in `src/coordinare/graph/nodes/monitor_performer.py` at `:3232-3233`, where `new_events` is already in hand, via one `record_many()` per poll — mapping source `type` to `progress` / `tool_use` / `thinking` / `cost`. Read the log with `state.get("activity_log")` and no-op when it is `None`.

      **Full attribution, or T020 fails.** `card_id` and `stage` are locals already in scope at the push site; `card_title` and `card_number` are **not** — read them from `state.get("current_card") or {}` (`title`, `issue_number`), the same access pattern the module already uses. T020 asserts all four fields, so taking only the two locals is the obvious way to get this half-right.

      **Push, not derive.** The per-session fanout invokes the graph on a *copy* of state (`daemon.py:1501-1513`, merged back only at `:1528`) and `monitor_performer` has no internal poll loop, so a watcher over `daemon.state` sees events only at writeback — after however long the rest of the cycle takes. Recording here emits in the same invocation that observed the batch, which is the earliest anything can, and is what makes SC-004 measurable (research R1 records the superseded derived design and why it was reversed). Do **not** re-add an event diff to the watcher.

      Hand `record_many` the whole reported batch without pre-diffing — backends may re-report their entire accumulated list (`monitor_performer.py:3543-3547`), and suppression is the log's job by content (FR-022, FR-023). A positional cursor would break anyway: the source list is a rolling `[-100:]`. (depends on T037)

- [X] T024 [US1] Extend `_watch_active_sessions()` in `src/coordinare/dashboard.py` to detect `performer_stage` and `phase` transitions and record `stage_change` entries naming the card and the stage entered, batched into one `record_many()` per tick. This is the watcher's *only* recording duty besides T026 — it suits the watcher because `_active_sessions_fingerprint` (`:275-298`) already tracks both fields. Capture the prior fingerprint into a local before `self._watcher_fingerprint = fp` overwrites it (`dashboard.py:324`) — the transition needs both sides, and the current code discards the old value. The existing `if fp == self._watcher_fingerprint: continue` early-return at `:322-323` stays exactly as it is: a stage change *always* moves the fingerprint, so unlike the abandoned event diff this work correctly belongs inside the branch. Do **not** add `performer_events` to the fingerprint — that would rebroadcast a full snapshot per agent event and change `state_update` cadence (FR-002).
- [X] T026 [US1] Call `forget_card()` from the watcher in `src/coordinare/dashboard.py` when a card leaves `active_sessions`, releasing its dedup bookkeeping while retaining its entries (FR-024). (depends on T024)
- [X] T027 [US1] Add the client silence timer in `src/coordinare/dashboard.py`: any received message (`state_update`, `activity_event`, or `heartbeat`) resets it; exceeding ~40 s marks the feed not-live (FR-026). Bind to the existing `#nav-sse-dot` and `#disconnected-banner` state rather than introducing a second indicator (research R4). (depends on T019)
- [X] T028 [US1] Render the not-live state in `src/coordinare/dashboard.py`: keep retained entries visible and mark them possibly out of date; never clear the feed on disconnect (FR-027). (depends on T027)
- [X] T029 [US1] Populate the empty-state element in `src/coordinare/dashboard.py` with text stating the feed covers only the current daemon run, so an empty feed after restart is not read as "nothing is happening" (FR-018). (depends on T017)

**Checkpoint**: US1 complete. The feed moves while work happens, holds still when it does not, and distinguishes still-and-live from still-and-dead.

---

## Phase 4: User Story 2 — Quiet, stall, and stuck warnings with no notification channel (Priority: P1)

**Goal**: The signals that say "this is not moving" reach the UI whether or not Slack exists — including on a completely stock install, where neither existing watchdog fires in useful time.

**Independent Test**: Configure zero notification channels. Confirm a card marked quiet within minutes, a stuck entry at its threshold, and a stall entry when the watchdog is enabled. Then configure a channel and confirm external dispatch is unchanged.

### Tests for User Story 2

- [X] T030 [P] [US2] Write `tests/integration/test_138_stall_stuck_to_feed.py` asserting that with **zero notification channels configured**, a card past its stuck threshold produces a `stuck` feed entry naming card, phase, and elapsed time (FR-009). This is the regression test for the reported bug. Assert the entry appears **and** that repeat firings stay bounded by `cooldown_seconds` with zero channels — the cooldown stamp moved in T038 is what makes that true, and it is the half of the bug that a naive gate removal reintroduces.
- [X] T031 [P] [US2] Add to `tests/integration/test_138_stall_stuck_to_feed.py` the stall case: with `stall_timeout_seconds` configured, a tripped watchdog produces a `stall` entry naming card, stage, and duration without progress (FR-010).
- [X] T032 [P] [US2] Add to `tests/integration/test_138_stall_stuck_to_feed.py` the auto-recovery case with zero channels configured, producing a `recovered` entry (FR-012).
- [X] T033 [P] [US2] Add to `tests/integration/test_138_stall_stuck_to_feed.py` the co-existence case: with a channel configured, the feed entry appears **and** the notification still dispatches, with dedup and cooldown behaviour unchanged (FR-011, FR-013).
- [X] T034 [P] [US2] Add to `tests/integration/test_138_stall_stuck_to_feed.py` an assertion that recording activity never alters a card's phase, retry counters, or terminal outcome (FR-031, SC-012).
- [X] T034a [US2] **Quiet-anchor gate (manual)** — run checks **C8 and C9** from the `quickstart.md` client-side checklist. C8: a card in `monitoring_performer` with an `agent_dispatch_at` older than the quiet threshold and **zero** retained entries is marked quiet (FR-028) — the post-restart scenario, which anchor-on-newest-entry-only fails silently by leaving the card unmarked forever. C9: a card with neither an entry nor an `agent_dispatch_at` is left unmarked rather than marked off a missing timestamp. Manual because the rule lives in inlined dashboard JS with no test runner — see the plan's Complexity Tracking deviation. Record the result in the PR description. (depends on T044)

### Wiring

- [X] T035 [P] [US2] Add `quiet_threshold_seconds: int = Field(default=300, ge=0)` to `StuckAlertConfig` in `src/coordinare/config.py` with a comment noting `0` disables and that it must stay below the smallest stuck threshold (FR-029).

> `CoordinareState` threading (T036, T037) moved to **Phase 2 Foundational** — US1's agent-event push (T023) needs it too, so it is no longer US2-only.

### Signal sites

- [X] T038 [US2] In `src/coordinare/daemon.py`, record a `stuck` entry inside the `if _elapsed > _threshold and _cooldown_ok:` block at line 3149, before the dispatch. Then make three changes that must land together (FR-009, FR-011, FR-013):
      1. Remove `and notification_service is not None` from the condition at line 3137. This is **hardening, not the fix** — the service is never `None` in production (see research verified facts); the removal only matters for tests and non-dashboard embeddings.
      2. Because that gate no longer protects it, wrap the `await notification_service.dispatch(...)` at line 3161 in its own `if notification_service is not None:`. There is no such inner guard today.
      3. Move `self._last_stuck_alert_at = monotonic()` (line 3177) out of the `try` and out of the dispatch guard, so the cooldown advances whether or not a channel exists. Left where it is, a `None` service raises into the `except` at `:3178`, the stamp never lands, and the feed takes a `stuck` entry every cycle — violating FR-013 and SC-006 in exactly the zero-channel case T030 tests. (depends on T037)
- [X] T039 [US2] In `src/coordinare/graph/nodes/monitor_performer.py`, record a `stall` entry at the watchdog trip (line 3560) with card, stage, and elapsed seconds, before the existing kill-and-retry path. Reading `state.get("activity_log")` must no-op when absent (FR-010). (depends on T037)
- [X] T040 [US2] In `src/coordinare/graph/nodes/check_board.py`, record a `recovered` entry before the `if notif is not None` branch at line 534 so recovery surfaces with no channel configured (FR-012). (depends on T037)
- [X] T041 [US2] Record an `error` entry on terminal error and blocked transitions in `src/coordinare/graph/nodes/monitor_performer.py`, carrying the reason (FR-006, US3 scenario 3). (depends on T037)

### Presentation

- [X] T042 [US2] Add severity CSS classes for `quiet`, `stall`, and `stuck` to `src/coordinare/dashboard.py` as new `--color-ev-*` custom properties beside the existing tokens at lines 713–715. No hard-coded colours (Constitution Principle III).
- [X] T043 [US2] Render a visible text severity prefix on quiet, stall, stuck, and error entries in `src/coordinare/dashboard.py`, so severity is never conveyed by colour alone (WCAG 1.4.1, research R5). (depends on T042)
- [X] T044 [US2] Implement client-side quiet detection in `src/coordinare/dashboard.py`: for each card in `active_sessions`, compare now against `last_heard = max(newest entry timestamp, agent_dispatch_at)` — skipping whichever is absent — using `activity_quiet_threshold_seconds` from the snapshot. Render a marker plus **one** synthetic row per quiet episode; clear on any new entry. Synthetic rows must never enter the stored log or count as progress (FR-028 through FR-033).

      **The `agent_dispatch_at` fallback is load-bearing, not defensive.** A card with no retained entry would otherwise be unevaluable, and the case that produces exactly that is a session restored after a daemon restart: the feed starts empty (FR-018) and a card restored straight into `monitoring_performer` produces no stage transition for T024 to observe. Anchoring on the newest entry alone leaves that card silent forever — the one card this feature exists to surface, after the restart most likely caused by it. `agent_dispatch_at` is already in each session summary (`dashboard.py:495-499`), so this is one `max()` and no new snapshot field, keeping FR-030 intact. When both are absent, do not mark the card. (depends on T016, T019)

**Checkpoint**: US2 complete. A stock install with no Slack surfaces a quiet card within minutes, and both watchdogs reach the UI when they fire.

---

## Phase 5: User Story 3 — Focus on one card (Priority: P2)

**Goal**: Narrow the feed to a single card and read enough retained history to understand how it reached its current state.

**Independent Test**: With two or more cards active, filter to one and confirm only its entries show and clearing restores the full feed. Confirm retained history spans a full stuck-threshold window.

### Tests for User Story 3

- [X] T045 [P] [US3] Write `tests/unit/test_138_activity_feed_filter.py` asserting `snapshot()` returns entries oldest-first by ascending `seq` (wire order, matching contract §1.2 invariant 1), that `limit` yields the newest N still oldest-first, and that per-card selection yields only that card's entries with clearing restoring all.
- [X] T046 [P] [US3] Add retention-window tests to `tests/unit/test_138_activity_feed_filter.py` asserting what the cap actually guarantees (FR-020, SC-005):
      1. At the **default** concurrency of 1, a 60-minute monitoring phase fits inside 2000 entries at the documented rate: the default 30 s poll interval gives ~120 polls, so assert that **16 new distinct events per poll** (1920 entries) leaves the whole window retrievable, and that **17 per poll** (2040) trims the oldest end. 2000 ÷ 120 ≈ 16 is the number Assumption 2 states; this test is what pins it.
      2. Past the cap, eviction is oldest-first and `len(entries) == 2000` exactly.
      3. At 20 cards the cap is a **shared** budget: a chatty card demonstrably ages out a quiet card's older entries, and that is asserted as expected behaviour, not a bug.

      Do **not** assert "at 20 concurrent cards the longest stuck window is still present" — it is false. 2000 is a memory budget (2000 × ~200 chars < 1 MB), not 20 × 100: the per-card 100-entry source cap bounds what is in flight at one instant, while the feed accumulates every distinct event over time, so one card can produce far more than 100 entries across a 60-minute phase. Spec Assumption 2 and SC-005 now say this plainly; the memory ceiling is the invariant and history depth is what gives.

### Implementation for User Story 3

- [X] T047 [US3] Add a card filter control to the feed panel in `src/coordinare/dashboard.py` as a native `<select>` — keyboard-reachable by default — populated from cards present in the feed plus an "All cards" option. (depends on T017)
- [X] T048 [US3] Apply the filter client-side in `src/coordinare/dashboard.py` so it survives live updates: a newly arriving entry for a filtered-out card must not appear, and clearing the filter must restore the full retained history without a refresh. (depends on T047, T019)

**Checkpoint**: All three user stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T049 **Accessibility gate (automated)** — add `tests/unit/test_138_contrast.py`: parse the `--color-*` custom-property declarations out of the dashboard CSS string in `src/coordinare/dashboard.py`, compute WCAG 2.1 relative-luminance contrast ratios for every `.ev-*` background/foreground pairing the feed reuses (`dashboard.py:801-806`) plus every severity class added in T042, and assert ≥ 4.5:1. Pure arithmetic — no browser, no new dependency, deterministic, and it fails on any future token edit that regresses contrast (Constitution Gate 7, research R6). Where a pairing fails, fix the **token value**, never a local override (Principle III).
- [ ] T049a **Accessibility gate (manual)** — keyboard and screen-reader verification per the Complexity Tracking deviation: filter reachable and operable by keyboard, live region announcing additions politely without re-reading existing rows. Record the result in the PR description.
- [X] T050 [P] Verify the feed panel's responsive behaviour in `src/coordinare/dashboard.py` at the dashboard's supported viewport sizes: the scroll container must not break the surrounding layout or clip the filter and liveness controls at the narrowest supported width (Constitution Principle III, responsive behavior).
- [X] T051 [P] Update `AGENTS.md` with a Recent Changes entry for 138 — the `activity_log` state field, the `activity_event` SSE kind, the stall/stuck/recovery entries that now reach the UI whether or not a notification channel is configured (the signal used to be lost at delivery, not detection), and the `quiet_threshold_seconds` setting.
- [X] T052 [P] Add the new configuration surface to the operator-facing config documentation, matching the reference table in `specs/138-dashboard-activity-feed/quickstart.md`.
- [ ] T053 Run the `quickstart.md` validation end to end: the zero-notification-channel walkthrough, the wedged-agent check, and the full **client-side checklist C1–C11**. **C1 (logic), C2, C4, C6–C11 and C3's threshold logic are now automated** by `tests/unit/test_138_client_js.py` (`make test-js`); what remains manual is the screen-reader half of C1, C3/C5 against a really-killed daemon, layout, and keyboard focus. Treat a skipped item as a failed gate rather than an untested nicety. C3 covers SC-010's 45-second budget and C6 covers SC-011. Record every outcome in the PR description.
- [X] T054 **SC-007 regression gate** — re-run `.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/dashboard/ tests/unit/test_dashboard_config_api.py -v` and confirm every pre-existing test still passes **unmodified**. If `test_sse_generator_yields_keepalive_on_timeout` in `tests/unit/test_dashboard.py` fails, the keepalive/heartbeat emission order was reversed (T014).
- [X] T055 Run `make lint` and `make test-all`; resolve every finding rather than suppressing it.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — **blocks all user stories**. Now includes the `CoordinareState` threading (T036, T037), moved up from Phase 4 because US1's agent-event push needs it too
- **US1 (Phase 3)**: depends on Foundational, including T036/T037
- **US2 (Phase 4)**: depends on Foundational. Independent of US1 in principle; T044 (quiet) reuses T019's rendering, so run US1 first if working sequentially
- **US3 (Phase 5)**: depends on Foundational. Filters whatever entries exist, so it is testable with US1 alone
- **Polish (Phase 6)**: depends on all desired stories; T049 and T049a require T042

### Critical path

```
T002 → T006 → T007 → T008 → T009 → T010 ──┐
                                           ├→ T015 → T037 ─┬→ T023  (agent events, US1)
T011 → T012 → T013/T014 ───────────────────┘        ▲       └→ T038/T039/T040/T041 (US2)
                                                    │
T036 ────────────────────────────────────────────────┘

T017 → T019 → T024 → T026        (panel, watcher — parallel to the push chain)
T016 → T044                      (quiet threshold → quiet detection)
```

`T011` (the `SSEBroadcaster` method) and `T036` (the `CoordinareState` field) are independent of the `ActivityLog` chain and can be built at any time before `T015`/`T037`.

Two orderings are not cosmetic:

- **T008 → T009** — truncation must run before the dedup key is built, or key size is unbounded and dedup diverges between long and short lines.
- **T010 + T011 → T015** — `T015` wires `activity_log.sink = broadcaster.broadcast_activity`, so both the sink attribute (T010) and `broadcast_activity` (T011) must exist first. Nothing reaches an open browser live until that wiring lands, so it is the single point where a mistake makes every push site look silently broken while every unit test still passes.

`T036 → T037` is on the critical path for **US1**, not just US2: T023 pushes agent events from a graph node, which needs `state["activity_log"]` populated.

### Within each user story

- Tests are written first and must fail before the implementation they cover
- `ActivityLog` before transport, transport before UI
- Story complete before moving to the next priority

### Parallel opportunities

- **Phase 2 tests** T003, T004, T005 — three independent files
- **Phase 2 implementation** T006–T010 (activity_log.py) is a serial chain; T011–T014 (SSE) is a serial chain; T036 (state.py) is genuinely parallel to both; T017–T018 (panel shell) can run alongside them, different regions of `dashboard.py`
- **US1 tests** T020, T021, T022, T025 — same file, so parallel authoring only
- **US2 tests** T030–T034 — same file, parallel authoring only. T034a is a *manual* gate, not an automated test, and runs after T044
- **US2/foundational wiring** T035 (config.py) and T036 (state.py) — different files, genuinely parallel
- **Polish** T050, T051, T052 — different files, genuinely parallel

⚠️ Two files concentrate this feature: `src/coordinare/dashboard.py` (transport, panel, watcher, quiet rendering) and `src/coordinare/graph/nodes/monitor_performer.py` (T023 agent events, T039 stall, T041 error — three separate regions of one file, so serialise them). Tasks touching either are marked `[P]` only where they modify clearly separate regions; when in doubt, serialise rather than risk a conflicted edit.

---

## Parallel Example: Phase 2 tests

```bash
# Three independent test files — author together, all must fail before Phase 2 implementation:
Task: "Write tests/unit/test_138_activity_log.py covering G1-G11 and invariants 1-7"
Task: "Add the three named behaviour tests to tests/unit/test_138_activity_log.py"
Task: "Write tests/unit/test_138_activity_feed_sse.py with the seven contract tests"
```

## Parallel Example: config + state wiring

```bash
# Different files, no shared state:
Task: "Add quiet_threshold_seconds to StuckAlertConfig in src/coordinare/config.py"
Task: "Add activity_log field to CoordinareState in src/coordinare/graph/state.py"
```

---

## Implementation Strategy

### MVP (US1 only)

1. Phase 1 Setup
2. Phase 2 Foundational — the bulk of the work; blocks everything
3. Phase 3 US1
4. **STOP and VALIDATE**: feed advances while an agent works, holds still when idle, reports not-live on disconnect
5. Demoable: answers "is anything happening?" without Slack

### Incremental delivery

1. Setup + Foundational → plumbing ready
2. US1 → feed moves (**MVP**)
3. US2 → quiet/stall/stuck visible with no channel — **this is the one that closes the reported bug**
4. US3 → per-card filtering and history depth
5. Polish → accessibility gate, docs, regression gate

If only one story past MVP ships, ship **US2**. US1 makes the dashboard nicer; US2 is why the feature exists.

### Parallel team strategy

Foundational is a mostly serial chain, so extra hands do not help much until it lands — though T011 (broadcaster) and T036 (state field) are genuinely independent and can be picked up early. After that: one developer on US1 (the `monitor_performer` agent-event push, the watcher's stage changes, liveness), one on US2 (signal sites + quiet), one on US3 (filter). Two collisions to sequence: US2's T044 touches rendering added in US1's T019, and T023/T039/T041 all edit `monitor_performer.py`.

---

## Notes

- `[P]` = different file, no dependency on an incomplete task
- Every call site reading `state.get("activity_log")` must no-op when it is `None` — tests and non-dashboard embeddings run without one
- The dedup key must never include `timestamp` or `seq`; including either silently disables suppression and makes a wedged agent scroll (the exact failure this feature exists to prevent)
- Keepalive comment stays byte-identical and stays first; the heartbeat is additive and second
- There is **one** gate removal, not two: the dead `notification_service is not None` check at `daemon.py:3137`. `check_board` (T040) removes nothing — the entry is recorded before the existing branch. Neither is the fix; the fix is that detection now has a destination that always exists
- T038 changes dispatch behaviour in two ways that must not be dropped: an inner `if notification_service is not None:` guard around the dispatch (new — the removed gate used to cover it), and the cooldown stamp moved out of the `try` so it lands whether or not a channel exists. Dedup, cooldown *duration*, and delivery semantics are otherwise untouched
- **Agent events are pushed, not derived** (T023). A watcher over `daemon.state` cannot meet SC-004: the fanout invokes the graph on a *copy* (`daemon.py:1501-1513`, merged at `:1528`) and `monitor_performer` polls once per invocation, so events become visible only at writeback. The watcher keeps the two jobs it already suits — stage transitions (T024) and `forget_card` (T026)
- **The sink is the only live path to the browser** (T010 + T015). Writers are graph nodes with no broadcaster; if `activity_log.sink` is unset, everything still records and evicts correctly and nothing reaches an open dashboard until the next reconnect. That failure is silent in unit tests and invisible in the log — T025 exists to catch it
- **FR-002 is additive-only, and one key is added**: `activity_quiet_threshold_seconds` (T016). Write T005's shape test to name that one addition, not to assert "identical to pre-138"
- **Quiet anchors on `max(newest entry, agent_dispatch_at)`** (T044). Entry-only anchoring leaves a restored post-restart card permanently unmarked — the exact card the feature exists for (T034a)
- **The inlined client-side JS is gated by `tests/unit/test_138_client_js.py`** — it slices the feed JS verbatim out of `_DASHBOARD_HTML` and executes it under node against a DOM stub (`tests/js/activity_feed_checks.js`). No browser, no npm install, no timers, no new dependency: node is already required by spec 124. It lives in `tests/unit/`, so CI runs it; `make test-js` runs it alone. This **narrows** the plan's Complexity Tracking deviation rather than honouring it — the original rationale conflated "needs a browser" with "cannot be executed". What still needs a human: screen-reader announcement, layout, keyboard focus (T049a). Everything server-side stays fully tested in Python; a skipped manual item is a failed gate
- **SC-004's 3 s budget covers pushed entries only** — agent activity, stall, stuck, recovered, error. Stage changes are cycle-granular by design (SC-004a), so do not write a tighter assertion for them than T022's
- Commit after each task or logical group
