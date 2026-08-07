# Phase 0 Research: Dashboard Activity Feed

All findings below were verified against the current tree on branch `138-dashboard-activity-feed` (base `main` @ 57f991d). Line references are to that state.

---

## R1 — How entries are sourced: derived, pushed, or both

**Decision (revised — see the superseded decision at the end of this section)**: **Everything that carries agent activity is pushed at the site that observes it.** Agent events are recorded in `monitor_performer` where `new_events` is already in hand (`:3232-3233`); stall, stuck, auto-recovery, and terminal error are pushed by their existing decision sites. All of them reach the log through one new `activity_log` field on `CoordinareState`. The 100 ms watcher keeps exactly two jobs, both of which suit it: detecting **stage transitions** (it already fingerprints `phase` and `performer_stage`) and calling `forget_card` when a card leaves `active_sessions`.

**Rationale**:

The original decision derived agent events from state in the watcher. That rested on two premises, and the code contradicts both.

*Premise 1 — "routine activity already lives in observable daemon state."* It does not, mid-cycle. The per-session fanout invokes the graph on a **copy**: `session_state = dict(self._state)` with the session deep-copied and flattened in (`daemon.py:1501-1513`), and the result is merged back only after `await graph.ainvoke(...)` returns (`:1528`). `monitor_performer` therefore mutates a copy, and `monitor_performer.py` has no internal poll loop (no `while True`, no `asyncio.sleep` — it polls once per graph invocation and returns). So `daemon.state["performer_events"]` and `active_sessions[cid]["performer_events"]` both only grow at writeback. A watcher polling at 100 ms detects the *writeback*, not the event — late by however long the rest of the cycle takes after `monitor_performer` returns, which is unbounded (GitHub calls, other nodes) and in practice far past SC-004's 3 s. Pushing at `:3232` emits at the instant the coordinare learns of the event, which is the earliest any UI can know.

*Premise 2 — "re-implementing diffing that the watcher already does."* The watcher does no event diffing. `_active_sessions_fingerprint` (`:275-298`) covers only `title`/`issue_url`/`pr_url`/`phase`/`performer_stage`/`container_id` — which is precisely why the derived design needed the diff hoisted above the early-return at `:322-323`. The diff was new code either way, and per tick it had to re-scan every active card's full rolling event list (FR-023 forbids a positional cursor), so ~20 cards × 100 entries of key-building at 10 Hz. The push site has the events already separated and needs no scan at all.

Pushing is also *less* code and better attributed: `card_id` and `stage` are locals at the push site, so FR-005 attribution is exact rather than derived, and it sidesteps the flat-vs-per-session ambiguity entirely (`performer_events` exists both as a top-level key — focus card only, `dashboard.py:422` — and as a per-session field in `_SESSION_FIELDS`, `session.py:238`; a derived reader has to pick correctly or silently drop every non-focus card's activity at concurrency > 1).

*Point-in-time decisions* were pushed in the original decision and stay pushed, for the reason already established: they are not recoverable from state. The stall watchdog (`monitor_performer.py:3557-3611`) kills the turn and either re-dispatches or blocks; by the time the watcher next looks, only the aftermath is visible. Reconstructing "a stall happened" from `phase == "blocked"` plus a substring match on `system_error_reason` would break whenever that message is reworded. Same for auto-recovery (`check_board.py:525-548`).

*Stage transitions* stay derived, because that is the one thing the watcher is already built for — the fingerprint tracks `phase` and `performer_stage` today, so a transition needs the prior value captured before `self._watcher_fingerprint = fp` overwrites it (`:324`) and nothing else.

Reaching the log from a graph node needs no new mechanism: `notification_service` is already a typed field on `CoordinareState` (`graph/state.py:102`), populated in the initial state dict (`__main__.py:844`), and read by nodes via `state.get("notification_service")` (e.g. `check_board.py:534`). Adding `activity_log` the same way is idiomatic and introduces no new pattern.

**Fan-out (the consequence that must not be dropped)**: once writers live in graph nodes, none of them can reach `SSEBroadcaster`. The original design had the watcher both record *and* broadcast, which left every pushed entry — stall, stuck, recovered, error — reaching an open browser only on the next reconnect backfill, defeating FR-009/FR-010/FR-012 and SC-001 for the operator who already has the dashboard open. Fix: `ActivityLog` carries one optional `sink` callback, set once by `DashboardStore` to `broadcaster.broadcast_activity`, and fired by `record`/`record_many` with the entries actually appended. Every writer then fans out without knowing SSE exists, and `broadcast_activity` is already non-blocking (`put_nowait` under `suppress(QueueFull)`), so FR-004 is unaffected.

**Alternatives considered**:

- *Derive everything from the watcher (the superseded decision).* Rejected on both premises above: mid-cycle state is a copy, so SC-004 is unreachable; and the diff was new per-tick work, not reuse.
- *Derive agent events, keep the 3 s budget, and extend the fingerprint to include `performer_events`.* Rejected — it would rebroadcast a full snapshot per agent event and change the `state_update` cadence FR-002 freezes, and it still would not see mid-cycle mutations that live on a copy.
- *Push agent events per event rather than per batch.* Rejected — `new_events` is already a list at the site, so one `record_many` call per poll is both cheaper and gives the client one SSE batch instead of N.
- *Subscribe the feed to `NotificationService.dispatch` as a single funnel.* Genuinely attractive: `card_stuck`, `card_auto_recovered`, `card_blocked`, and `performer_error` all pass through it, and its dedup/cooldown would satisfy FR-013 for free. **Rejected on scope**, not merit — it only works if the service always exists, and "coordinare runs cleanly with zero channels configured" is explicitly spec 139. Taking it here would pull 139's startup and validation work into 138. Worth revisiting once 139 lands.

---

## R2 — Multiplexing `activity_event` onto the existing SSE stream

**Decision**: Keep one `/events` stream and one broadcaster queue. Tag activity payloads with a reserved `_event` key; `sse_stream` inspects it to choose the SSE event name. Snapshot payloads are untouched and continue to be emitted as `state_update`.

**Rationale**:

`SSEBroadcaster` queues carry bare `dict` payloads (`dashboard.py:182`) and `sse_stream` formats every one of them as `state_update` (`dashboard.py:363`). The minimal correct change is a discriminator on the payload rather than a change to the queue's type, because the latter would ripple into all four existing `broadcast()` call sites (`dashboard.py:326`, `daemon.py:3054`, `daemon.py:3281`, plus the initial send).

`build_snapshot` returns a dict of known top-level keys (`dashboard.py:370-576`); none is named `_event`, and the leading underscore keeps it clearly reserved, so there is no collision risk with a future snapshot field.

A second stream was rejected: it would double the per-client connection count, need its own keepalive and reconnect handling, and gain nothing — the events already share a natural ordering and a single consumer.

**Alternatives considered**:

- *Separate `/activity` SSE endpoint.* Rejected — duplicate connection lifecycle, keepalive, and disconnect handling for no benefit.
- *Change the queue payload to `tuple[str, dict]`.* Rejected — touches every existing call site and every existing broadcaster test for a purely cosmetic gain over the discriminator key.
- *Fold entries into `state_update`.* Rejected outright — violates FR-001 (append-only stream, not whole-snapshot refresh) and would grow every snapshot by the retained history.

---

## R3 — Duplicate suppression structure

**Decision**: Per-card `set[str]` of entry keys for O(1) membership, paired with a `deque[str]` of the same keys for FIFO eviction, both bounded at 256 keys per card. The key is the truncated `(type, text)` pair itself — no hashing. Both structures are dropped when the card leaves `active_sessions` (FR-024).

**Rationale**:

The spec requires suppression that survives a source re-reporting its whole list (FR-023). The volume that has to be checked is real: at the supported maximum of 20 concurrent cards (`config.py:851`) with a 100-entry source cap each, a naive linear scan is ~2000 membership tests per watcher tick at 10 Hz. A set makes each test O(1) and removes the question entirely.

Storing the key verbatim rather than a digest is both cheaper and easier to debug — because lines are already truncated to ~200 characters at ingest (FR-035), a key is small and bounded, so hashing would add CPU and opacity for no memory saving. 256 keys per card comfortably exceeds the 100-entry source cap, so a card cannot evict its own live window and re-emit within a single turn.

The paired-structure idiom is needed because `set` has no ordering; the deque supplies eviction order while the set supplies the lookup. This is the standard bounded-LRU-without-a-dependency shape.

**Alternatives considered**:

- *`deque` alone with `in` checks.* Rejected — O(n) per check at the volumes above.
- *`OrderedDict` as an LRU.* Equivalent behavior and slightly less code, but it obscures that keys are a set membership question, not a mapping; rejected on readability (Principle I). Acceptable if the implementer prefers it — behavior is identical.
- *Global seen-set instead of per-card.* Rejected — one chatty card would evict another's keys and cause spurious re-emission; per-card also makes FR-024's release trivial.
- *Content digest (SHA/hash) as key.* Rejected — no memory saving over an already-truncated line, and it makes debugging a dedup miss much harder.

---

## R4 — Client-visible liveness without breaking the keepalive contract

**Decision**: Keep the existing `": keepalive\n\n"` comment exactly as the first yield on timeout, and emit an additional named `heartbeat` event immediately after it. The browser binds feed liveness to the existing `#nav-sse-dot` / `#disconnected-banner` state plus a client-side silence timer reset by any received message.

**Rationale**:

Most of FR-025/FR-026 is already built and must not be rebuilt: `dashboard.py:3479-3504` wires `es.onerror` and `es.onopen` to a disconnected banner and a nav dot. That covers a *hard* drop.

The residual gap is a *silent* stream — daemon wedged, TCP still open, no data flowing. `es.onerror` will not fire for a long time in that case, and the existing 15 s keepalive cannot help the client detect it, because **SSE comment lines are consumed by the browser's parser and never surface to JavaScript**. There is no `EventSource` API that exposes them. So client-side silence detection requires a *named event*, not a comment.

Replacing the comment is not an option: `tests/unit/test_dashboard.py:867-890` asserts the exact string `": keepalive\n\n"` as the first yield after a timeout, and SC-007 requires existing tests to pass unmodified. Yielding the comment first and the heartbeat second satisfies both — the test's single `__anext__()` still receives the comment, and the client gets a JS-visible signal 15 s apart.

**Alternatives considered**:

- *Replace the comment with a named event.* Rejected — breaks `test_dashboard.py:890`, violating SC-007.
- *Heartbeat first, comment second.* Rejected — same test breakage; order matters because the test reads exactly one item.
- *Infer liveness from `last_poll_at` in the snapshot.* Rejected — that measures *daemon* liveness, not *stream* liveness, and an idle daemon would look disconnected.
- *No heartbeat; rely on `onerror` alone.* Rejected — leaves exactly the silent-stall case that FR-026 names, which is the case most likely to mislead an operator.

---

## R5 — Accessible live region (Constitution Principle III, Check A)

**Decision**: The feed container is `role="log"` with `aria-live="polite"` and `aria-relevant="additions"`. Only the newest entries are inserted (never a full re-render of the list), so assistive technology announces additions rather than re-reading the feed. Stall, stuck, and error entries carry a visible text severity prefix, not colour alone. The card filter is a native `<select>`, keyboard-reachable by default.

**Rationale**:

`aria-live="polite"` is the correct register for a status feed — it waits for a pause rather than interrupting, which `assertive` would do on every progress tick and make the dashboard unusable with a screen reader. `role="log"` carries the "newest at one end, append-only" semantic exactly.

The append-only insertion rule matters as much as the attributes: re-rendering the whole list would make `aria-relevant="additions"` announce every existing row again. This constrains the implementation, so it is called out here rather than discovered later.

The severity prefix satisfies WCAG 1.4.1 (Use of Colour) — FR-014 asks for visual distinction, and colour alone would not meet AA.

**Alternatives considered**:

- *`aria-live="assertive"`.* Rejected — interrupts the user on every tick.
- *No live region.* Rejected — a live-updating panel invisible to assistive tech fails Principle III's explicit WCAG 2.1 AA requirement.
- *Colour-only severity.* Rejected — fails WCAG 1.4.1.

---

## R6 — Contrast verification for reused `.ev-*` styling (Constitution Principle III, Check B)

**Decision**: Reuse the existing `.ev-*` classes per FR-017, and add a verification step in Phase 2 that measures each reused pairing against WCAG 2.1 AA (4.5:1 for body text). Where a pairing falls short, adjust the **token value** rather than hard-coding a local override, so every existing consumer of that token benefits.

**Rationale**:

FR-017 (reuse existing styling) and Principle III (WCAG 2.1 AA) can conflict, and the honest position is that the existing pairings are unverified rather than known-good. The relevant declarations are at `dashboard.py:801-805`, pairing dark backgrounds such as `--color-ev-progress: #1a2a1a` (`:713`) with accent foregrounds like `--color-accent-green`.

Fixing at the token level keeps the design-token rule intact — Principle III forbids hard-coded values, so a local override to dodge a contrast failure would trade one violation for another. It also means any contrast fix improves the existing Active Performers panel at the same time.

This is scoped as *verify, then fix only what fails*. It is deliberately not a restyle of the dashboard, which the spec puts out of scope.

**Alternatives considered**:

- *Assume the existing palette passes.* Rejected — Principle III requires accessibility checks to pass, and assumption is not verification.
- *New feed-specific colour set.* Rejected — contradicts FR-017 and fragments the token system.
- *Local overrides where contrast fails.* Rejected — violates the design-token rule.

---

## Resolved unknowns

No `NEEDS CLARIFICATION` markers were carried into this plan. The five spec-level ambiguities were settled during `/speckit.clarify` and are recorded in the spec's Clarifications section; the six decisions above are implementation-level and were resolved here against the code.

## Verified facts this plan depends on

| Fact | Location | Consequence |
|---|---|---|
| Stuck detection carries a `notification_service is not None` gate, but the service is never `None` in production — `build_notification_service` returns a service even with zero channels | `daemon.py:3137`; `services/notification.py:315-342`; `__main__.py:752, 844` | Detection already runs with no channel configured. The gate is dead in the production wiring and is removed as hardening (FR-009), **not** as the fix |
| The signal is lost at delivery, not detection: `dispatch()` finds no routing and returns after logging `notification_unrouted` | `services/notification.py:193-206` | **This is the reported bug's actual root cause.** The fix is the activity entry, which gives detection a destination that exists unconditionally |
| Auto-recovery detection is likewise ungated — only the dispatch at `:534` is guarded | `check_board.py:527-535` | FR-012's entry must be recorded before the `if notif is not None` branch; no gate removal needed |
| Stall watchdog defaults to disabled | `config.py:1675` (`stall_timeout_seconds: 0`) | Stall entries are opt-in; not the default-config answer |
| Stuck threshold is 3600 s for both monitoring phases | `config.py:311-312` | Stock install would wait an hour — why FR-028 (quiet) exists |
| Card concurrency ceiling is 20, default 1 | `config.py:851` | Retention is a **shared** budget across cards. 2000 is *not* derived from 20 × 100 — the source cap bounds what is in flight at an instant, while the feed accumulates over time. FR-020/SC-005 scope the history guarantee to the default concurrency; the memory ceiling is what holds at 20 |
| Source events are capped at 100 per card | `monitor_performer.py:3233` | Per-card seen-set of 256 cannot evict a live window |
| The per-session fanout invokes the graph on a **copy** of state and merges back only after `ainvoke` returns | `daemon.py:1501-1513`, `:1528` | Mid-cycle mutations are invisible to a watcher over `daemon.state`. Agent events must be **pushed** at `:3232`, not derived (R1) |
| `monitor_performer` has no internal poll loop — one poll per graph invocation | no `while True` / `asyncio.sleep` in the module | Event visibility is gated by cycle cadence, not by the watcher's 100 ms; `poll_interval_seconds` defaults to 30 (`config.py:817`) |
| `performer_events` exists both as a top-level key and as a per-session field | `dashboard.py:422` (flat, focus card only); `session.py:238` (`_SESSION_FIELDS`) | A derived reader must pick the per-session copy or silently drop non-focus cards; pushing at the source avoids the choice |
| Each active session summary already reports `agent_dispatch_at` | `dashboard.py:495-499` | Quiet detection's fallback anchor for a card with no retained entry (FR-028) needs **no new snapshot key** |
| Backends may re-report their whole event list each poll | `monitor_performer.py:3543-3547` | Dedup is mandatory, not an optimisation (FR-022) |
| Keepalive string is asserted exactly | `tests/unit/test_dashboard.py:890` | Heartbeat must be additive and second (R4) |
| Disconnect indicator already exists | `dashboard.py:3479-3504` | Extend, do not rebuild (FR-025) |
| `notification_service` threading pattern | `graph/state.py:102`, `__main__.py:844` | Template for `activity_log` |
