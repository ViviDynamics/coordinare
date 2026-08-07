# Implementation Plan: Dashboard Activity Feed

**Branch**: `138-dashboard-activity-feed` | **Date**: 2026-07-30 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/138-dashboard-activity-feed/spec.md`

## Summary

Add a bounded, in-memory activity log to the daemon and render it as a live, newest-first, per-card-filterable feed in the dashboard, so an operator can tell "working" from "wedged" without Slack.

The approach is deliberately additive. A new `ActivityLog` service owns retention, per-entry truncation, and duplicate suppression. Entries are **pushed at the site that observes them** — agent events in `monitor_performer` where the batch is already in hand (`:3232-3233`), and stall, stuck, auto-recovery, and terminal error at their existing decision sites — all reached through a new `activity_log` field on `CoordinareState`, mirroring how `notification_service` is already threaded (`graph/state.py:102`, `__main__.py:844`). The one difference: `notification_service` is *built inside* `_bootstrap_services`, whereas the `ActivityLog` is owned by `DashboardStore` and must be passed in — `_bootstrap_services` gains a keyword parameter for it (T037). The existing 100 ms `active_sessions` watcher keeps two jobs it is already shaped for: **stage transitions** (it fingerprints `phase` and `performer_stage` today) and releasing per-card dedup state when a card leaves `active_sessions`. No new polling loop, and no derived event diff — research R1 records why the derived variant cannot meet SC-004 (the fanout hands each graph invocation a *copy* of state, so mid-cycle mutations are invisible until writeback).

Because writers now live in graph nodes that cannot see `SSEBroadcaster`, `ActivityLog` carries one optional `sink` callback, set once by `DashboardStore` to `broadcaster.broadcast_activity` and fired with whatever `record`/`record_many` actually appended. Every writer fans out live without knowing SSE exists; `broadcast_activity` is already non-blocking, so FR-004 holds. Without it, pushed entries would reach an open browser only on the next reconnect backfill.

Entries arrive as a new `activity_event` SSE kind alongside `state_update`, which gains exactly one additive key (`activity_quiet_threshold_seconds`) and is otherwise byte-identical. Quiet detection is computed client-side, which makes FR-031 ("observation, not intervention") structurally guaranteed rather than merely asserted; it anchors on the card's newest entry or, for a card with none — a session restored after a restart — the `agent_dispatch_at` the snapshot already reports (`dashboard.py:495-499`), so no new snapshot field is needed for it.

The reported bug is a *delivery* failure, not a detection failure: with zero channels the stuck decision is still made, then handed to a notification service with nothing to route it to and dropped (`services/notification.py:193-206`). The fix is therefore the activity entry — a destination that exists unconditionally. Two call-site gates are also removed, but as hardening for the `None`-service path in tests and non-dashboard embeddings, not as the fix. That is the whole of this spec's overlap with 139.

## Technical Context

**Language/Version**: Python 3.12+ (prod 3.14.5 via uv); dashboard front end is vanilla ES5-style JS inlined in `dashboard.py`
**Primary Dependencies**: Existing only — FastAPI + Starlette (SSE), asyncio, `collections.deque`, pydantic 2.x, structlog. **No new dependency.**
**Storage**: None. Bounded in-memory `deque`, per-process, discarded on restart (FR-019).
**Testing**: pytest + `pytest-asyncio`; `tests/unit/test_138_*.py`, `tests/integration/test_138_*.py`
**Target Platform**: Linux server, single-host single-process daemon; dashboard in a modern browser via `EventSource`
**Project Type**: Single project (`src/coordinare/`), with the front end inlined in the dashboard module
**Performance Goals**: Agent-activity and point-in-time entries visible in an open dashboard within 3 s of the **coordinare observing** the event (SC-004) — measured from the push site, since the coordinare cannot know sooner than its own performer poll (`poll_interval_seconds`, default 30 s). Stage changes are cycle-granular by design and excluded (SC-004a). Quiet marker within minutes on stock config (SC-011)
**Constraints**: Hard memory ceiling ≈ 2000 entries × ~200-char line ≈ **under 1 MB** (SC-013); zero added latency to the daemon cycle (FR-004); no change to card outcomes (SC-012); existing dashboard tests pass unmodified (SC-007)
**Scale/Scope**: ≤ 20 concurrent cards (`config.py:851`), each with a 100-entry source event cap

## Constitution Check

*GATE: evaluated before Phase 0 and re-evaluated after Phase 1 design.*

| Principle | Status | Evidence / how the design satisfies it |
|---|---|---|
| **I. Code Quality First** | **PASS** | New logic goes in `services/activity_log.py` (~150 lines, one responsibility: bounded deduplicated activity retention) rather than growing the 5107-line `dashboard.py`. No new dependency — `deque` and `set` from stdlib. Type annotations on all public surfaces; `from __future__ import annotations` per house style. |
| **II. Testing Discipline** | **PASS with one recorded deviation** | `ActivityLog` is a plain class with no FastAPI or asyncio coupling, so retention, truncation, dedup, eviction, and the sink fan-out are directly unit-testable and deterministic. Integration tests cover stall→feed, stuck→feed, and the agent-event push with **zero notification channels** (SC-008). No existing test is modified — the exact `": keepalive\n\n"` contract at `test_dashboard.py:890` is preserved (see research R4). **Deviation**: the inlined client-side JS has no test runner in this repo and is verified manually against a `quickstart.md` checklist; scope, rationale, and residual risk are in Complexity Tracking. Everything expressible in Python is tested in Python. |
| **III. UX Consistency** | **PASS with two required checks** | Reuses existing `.ev-*` classes and CSS custom properties, so design tokens are honoured and no hard-coded colors are introduced (FR-017). Loading/state feedback is explicitly in scope via the liveness indicator (FR-025). **Check A**: the feed is a live region and must announce politely without flooding a screen reader — resolved in research R5. **Check B**: existing `.ev-*` contrast must be verified against WCAG 2.1 AA, and any new severity styling must meet it — resolved in research R6 and automated as a unit test (T049). Keyboard/screen-reader verification stays manual (T049a); see Complexity Tracking. |
| **IV. Performance by Design** | **PASS** | Budgets are in the spec's Success Criteria as the constitution requires: SC-004 (3 s from observation to visible), SC-013 (memory ceiling), SC-012 (no outcome change). SC-004 is **measured, not assumed** — pushing at `monitor_performer:3232` means the entry is emitted in the same invocation that observed the event, and T020 asserts it there rather than by injecting a state mutation (which would have measured nothing). The memory ceiling is enforced by construction — `deque(maxlen=…)` plus ingest-time truncation — and asserted by unit test rather than a CI benchmark; see Complexity Tracking for that deviation. Dedup is O(1) per event via a set, and pushing removes the per-tick re-scan of every card's rolling event list that the derived design required. |
| **V. Clarity Before Action** | **PASS** | Five clarifications resolved and recorded in the spec's Clarifications section. Zero `NEEDS CLARIFICATION` markers remain in spec or plan. |

**Gate result: PASS.** No unjustified violations. Three deviations are recorded in Complexity Tracking: the SC-013 unit assertion in place of a CI benchmark, manual keyboard/screen-reader verification alongside automated contrast, and manual verification of the inlined client-side JS.

## Project Structure

### Documentation (this feature)

```text
specs/138-dashboard-activity-feed/
├── plan.md              # This file
├── spec.md              # Feature specification (37 FRs, 13 SCs)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── activity-event.md   # SSE event + ActivityLog API contract
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   └── activity_log.py          # NEW — ActivityEntry, ActivityLog (retention, dedup,
│                                #   truncation, sink fan-out)
├── dashboard.py                 # SSE activity_event + backfill + heartbeat; ActivityLog
│                                #   ownership + sink wiring; watcher-derived stage_change
│                                #   and forget_card; feed panel HTML/CSS/JS; quiet
│                                #   threshold — the one additive snapshot key
├── daemon.py                    # Push stuck entry (:3149); add inner dispatch guard;
│                                #   move cooldown stamp out of the try (:3177); drop the
│                                #   dead `notification_service is not None` gate (:3137)
├── graph/
│   ├── state.py                 # NEW field: activity_log (mirrors notification_service :102)
│   └── nodes/
│       ├── monitor_performer.py # Push agent-event batch where new_events is already in
│       │                        #   hand (:3232-3233); push stall entry at the watchdog
│       │                        #   trip (:3560); push terminal error/blocked
│       └── check_board.py       # Push auto-recovered entry before the existing
│                                #   `if notif is not None` branch (:534) — no gate removed
├── __main__.py                  # Thread the DashboardStore-owned ActivityLog into the
│                                #   initial state: new kwarg on _bootstrap_services,
│                                #   passed at the call site (:959), set beside
│                                #   notification_service (:844) — not constructed here
└── config.py                    # quiet_threshold_seconds on StuckAlertConfig (:305)

tests/
├── unit/
│   ├── test_138_activity_log.py       # retention, eviction, truncation, dedup, seen-set bound, sink
│   ├── test_138_activity_feed_sse.py  # activity_event shape, backfill on connect, heartbeat
│   ├── test_138_activity_feed_filter.py # snapshot ordering, per-card selection, retention window
│   └── test_138_contrast.py           # WCAG 2.1 AA contrast over the --color-* tokens (Gate 7)
└── integration/
    ├── test_138_activity_feed_live.py  # pushed agent events (same invocation, no cycle wait),
    │                                   #   wedged agent, stage change, sink fan-out
    └── test_138_stall_stuck_to_feed.py # stall→feed, stuck→feed, auto-recovered→feed,
                                        #   all with zero notification channels
```

**Structure Decision**: Single project, matching the existing layout. The one new module is `src/coordinare/services/activity_log.py`, placed alongside peer services (`notify.py`, `retry_counter.py`, `dispatch_guard.py`) because it is pure state management with no web-framework coupling — which is also what makes it cleanly unit-testable per Principle II. Everything else is an edit to an existing file, and the front end stays inlined in `dashboard.py` as the project already does.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| Principle IV asks for automated CI benchmarks on performance-critical paths; the memory ceiling (SC-013) is enforced by a unit assertion instead | The budget here is a **structural invariant** (`maxlen` × truncation), not a latency curve that can regress gradually. A unit test asserting the deque never exceeds its cap and lines never exceed the limit fails deterministically the moment the invariant breaks. | A CI benchmark harness would add timing-sensitive infrastructure for a bound that cannot drift without an explicit code change, and timing-based tests risk the flakiness Principle II forbids. |
| Principle III / Quality Gate 7 asks for automated accessibility scans on UI changes; keyboard and screen-reader behaviour is verified manually (T049a) while contrast is automated (T049) | An automated scan needs axe-core, which is a new JS dependency that cannot be CDN-loaded under the dashboard's offline/CSP constraints, and `pytest-playwright` alone does not evaluate ARIA semantics. Contrast — the one criterion that can silently regress from a token edit — is fully automated as a dependency-free unit test. | Adding axe-core plus a browser fixture would introduce a new dependency and a timing-sensitive browser test for two checks (focus order, live-region politeness) that change only when the markup changes, and the markup is fixed by T017. Manual verification of a static structure is honest; a flaky browser test is not (Principle II). |
| Principle II requires unit tests for every public function; for the dashboard's **inlined client-side JavaScript**, *rendering, screen-reader announcement behaviour and keyboard focus order* are verified **manually** (T049a). The **logic** — feed append and `seq` dedup (T019), the silence timer (T027), not-live rendering (T028), quiet detection (T044), the card filter (T047, T048) — is **automated** by `tests/unit/test_138_client_js.py` | The front end is vanilla ES5-style JS inlined in a Python string in `dashboard.py`: no JS module, no bundler, no build step. **Implementation note — this row was narrowed during implementation.** The original plan put the whole front end on the manual checklist, on the grounds that a JS runner meant a new dependency plus a timing-sensitive browser suite. That turned out to be a false dichotomy: the *logic* can be executed without a browser by slicing the feed JS verbatim out of `_DASHBOARD_HTML` and running it under node against a ~60-line DOM stub (`tests/js/activity_feed_checks.js`). No browser, no npm install, no timers, no new dependency — node 22 is already required by spec 124 (openwiki). 45 checks cover C1, C2, C4, C6–C11 and C3's timing logic, and the gate is mutation-verified: breaking the quiet anchor, removing the `seq` dedup, or swapping the append for a full re-render each fail it. What genuinely needs a browser stays manual and is now a much smaller surface. | A **browser** suite (`pytest-playwright`) is still rejected — a new dependency and timing-sensitive tests for behaviour that changes only when the markup changes, the same trade rejected for axe-core one row above. Extracting the JS into a real module with a bundler is still rejected — a front-end toolchain for one panel is a far larger change than the feature. What was *wrongly* rejected was executing the shipped JS as-is: it needs neither. Residual risk is now confined to what a human must look at — layout, ARIA announcement, focus order — and T049a owns it. |

## Phase 0 — Research

Complete. See [research.md](./research.md). Six decisions resolved: derived-vs-pushed entry sourcing (R1), SSE multiplexing without disturbing `state_update` (R2), dedup structure (R3), heartbeat that preserves the existing keepalive test (R4), accessible live-region behavior (R5), contrast verification (R6).

## Phase 1 — Design & Contracts

Complete. See [data-model.md](./data-model.md), [contracts/activity-event.md](./contracts/activity-event.md), and [quickstart.md](./quickstart.md).

## Post-Design Constitution Re-Check

Re-evaluated after Phase 1, and again after cross-artifact analysis. **Still PASS.** The analysis pass revised R1 — agent events moved from watcher-derived to pushed at `monitor_performer:3232` — because the fanout hands each graph invocation a copy of state (`daemon.py:1501-1513`), making SC-004 unreachable by derivation and making the *only* test of it blind to the failure. The revision strengthens Principle IV (the budget is now measurable at the emission site) and Principle I (less code: no per-tick event diff, no flat-vs-per-session choice, attribution from locals). It also surfaced that pushed entries had no fan-out path at all, fixed by the `ActivityLog.sink` callback. The design still adds no dependency, no persistence, and no new background task. The two Principle III checks flagged before Phase 0 are now resolved with concrete decisions (R5 live-region policy, R6 contrast verification step); R5 shapes the Phase 2 markup and rendering tasks (T017, T019), and R6 is enforced by the Phase 6 accessibility gate (T049, T049a). Complexity Tracking carries two entries: the SC-013 unit assertion in place of a CI benchmark, and manual keyboard/screen-reader verification alongside the automated contrast test.
