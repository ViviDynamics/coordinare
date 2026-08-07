# Feature Specification: Dashboard Activity Feed

**Feature Branch**: `138-dashboard-activity-feed`
**Created**: 2026-07-30
**Status**: Draft
**Input**: User description: "Give developers a live activity feed in the dashboard so they can tell at a glance whether the agents are making progress or stuck — the way Claude Code / Codex narrate what they're doing in the terminal. Today the dashboard shows current state (which card is on which stage, tokens, cost), but there is no chronological, human-readable 'here's what just happened' narrative, and the one signal that says 'this stalled' (`card_stuck`) only leaves via notification channels (Slack/email), not the UI."

## Overview

An operator watching the coordinare dashboard can see **what state the system is in** but not **whether it is moving**. A card that has sat in `monitoring_performer` for forty minutes looks identical whether the agent is grinding through a large refactor or wedged on a hung model read. The terminal agents (Claude Code, Codex) solve this by narrating their work continuously; the dashboard has the underlying data to do the same but never assembles it into a chronological narrative.

The reported failure is sharper than "no narrative", though. The signal that is *supposed* to tell an operator a card has stalled — the stuck-card alert — has exactly one delivery path, and that path is a notification channel. A developer who disabled the Slack connector during setup does not get a degraded version of that signal; they get **no** signal. The detection itself still runs and still decides the card is stuck; the decision is then handed to a notification service that has no channel to route it to, and it is dropped without trace. The stall watchdog and auto-recovery detection behave the same way. This feature separates *detecting* that a card is stalled or stuck from *delivering* that news to an external channel, and gives the detection a second, permanent, always-present destination: the dashboard UI.

## Clarifications

### Session 2026-07-30

- Q: When the same underlying agent event is observed repeatedly across polls, what should the feed do? → A: Content identity per event — emit only unseen events, tracked by a bounded per-card seen-set
- Q: How should the feed distinguish a dropped stream from a genuinely quiet system? → A: Show a liveness indicator on the feed; when the stream drops or goes silent past the expected keepalive, say so visibly
- Q: On a stock install the stall watchdog is disabled and the stuck threshold for the monitoring phases is 60 minutes — how should the feed close that blind spot? → A: Feed-derived quiet indicator; mark a card quiet when it has produced no entry for a threshold well under the stuck threshold, using only retained entries
- Q: How should retention be bounded, given "normal activity volume" was never quantified? → A: Fixed entry-count cap sized to the worst case (~2000 total), giving a hard memory ceiling and a concrete number to test against
- Q: A count cap alone does not bound memory if entry text is unbounded — how should per-entry size be handled? → A: Truncate each line at ingest to a fixed length (~200 characters); store only the truncated form

**Refinement (post-plan, cross-artifact analysis).** The quiet answer above said "using only retained entries". That leaves a card with *no* retained entry — a session restored after a daemon restart — permanently unmarkable, which is the exact case this feature exists for. Quiet is now anchored on the newest retained entry **or** the card's dispatch timestamp already present in the snapshot, whichever is later (FR-028, FR-030). Still feed-side, still zero new daemon detection, still no new snapshot field.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Tell "working" from "wedged" at a glance (Priority: P1)

An operator has the dashboard open while several cards move through the pipeline. They want to answer one question without reading logs or opening a container: *is anything actually happening right now?*

The dashboard presents a chronological, newest-first feed of what the agents have just done — a stage was entered, a tool was used, tokens were consumed, a turn finished. While an agent works, new lines arrive on their own and the feed visibly advances. When an agent is quiet, the feed is quiet, and the most recent line's timestamp tells the operator how long the quiet has lasted.

**Why this priority**: This is the core of the request and delivers value even with nothing else built. A feed that merely moves while work happens already lets an operator distinguish "slow" from "dead", which is the distinction they reported being unable to make.

**Independent Test**: Run the daemon against a card that produces agent activity, open the dashboard, and confirm new feed entries appear without refreshing the page and that each carries a readable description and a timestamp. Leave an idle system running and confirm the feed stays still and the newest timestamp visibly ages.

**Acceptance Scenarios**:

1. **Given** the dashboard is open and an agent is actively working on a card, **When** the agent emits progress, uses a tool, or accrues cost, **Then** a corresponding entry appears at the top of the feed within a few seconds with no manual refresh.
2. **Given** the dashboard is open, **When** a card enters a new pipeline stage, **Then** a feed entry records the transition, naming the card and the stage entered.
3. **Given** the dashboard is open and no agent is working, **When** the operator observes the feed, **Then** no new entries appear, the newest entry's displayed age increases over time, and the feed still reports itself as live — so stillness reads as "idle", not "disconnected".
4. **Given** an operator opens the dashboard partway through a run, **When** the page loads, **Then** the feed is immediately populated with the retained recent history rather than starting empty.
5. **Given** the dashboard is open and the stream is then interrupted, **When** the operator observes the feed, **Then** it reports itself as no longer live and marks its retained entries as possibly out of date.

---

### User Story 2 - See quiet, stall, and stuck warnings with no notification channel configured (Priority: P1)

An operator runs coordinare with notifications disabled — no Slack, no email. An agent goes silent, a card sits in one phase past its stuck threshold, or a turn makes no forward progress past the stall threshold. Today none of these produce anything the operator can see. After this change each produces a prominent, distinctly styled entry in the activity feed, and the fastest of them — the quiet marker — needs no configuration at all.

**Why this priority**: This is the specific failure the feedback describes and the reason the feature exists. It is co-P1 with Story 1 because a feed that moves but still goes silent on the one condition the operator cares about most would not answer the report. The quiet marker carries most of this weight in practice, since it is the only one of the three that fires on a stock install.

**Independent Test**: Configure the daemon with zero notification channels, drive a card past its stuck threshold, and confirm a stuck entry appears in the feed. Repeat with a configured stall timeout and a non-progressing agent turn, and confirm a stall entry appears. Separately, on stock configuration, let an active card fall silent past the quiet threshold and confirm it is marked quiet. None of these require a notification channel to be reachable or present.

**Acceptance Scenarios**:

1. **Given** a stock install — no notification channel, stall watchdog at its disabled default, stuck thresholds unchanged — **When** an active card produces no activity for longer than the quiet threshold, **Then** it is marked quiet in the feed within minutes, without waiting for the 60-minute stuck threshold.
2. **Given** a card has been marked quiet, **When** it emits any new activity, **Then** the quiet marking clears and the card's phase, retry budget, and outcome are unaffected by having been marked.
3. **Given** no notification channel is configured, **When** a card remains in a non-idle phase longer than its stuck threshold, **Then** a stuck entry appears in the feed identifying the card, the phase, and how long it has been there.
4. **Given** at least one notification channel *is* configured, **When** the same condition occurs, **Then** the feed entry appears **and** the external notification is dispatched as it is today — the feed does not replace or suppress existing delivery.
5. **Given** a stall timeout is configured and an agent turn makes no forward progress past it, **When** the stall watchdog trips, **Then** a stall entry appears in the feed naming the card, the stage, and the duration without progress.
6. **Given** a stuck entry is already in the feed for a card and phase, **When** the condition persists across many cycles, **Then** the feed is not flooded with a repeated entry every cycle.
7. **Given** a stall or stuck entry is in the feed, **When** the operator scans the feed, **Then** that entry is visually distinguishable from routine progress entries.

---

### User Story 3 - Focus on one card and understand how it got here (Priority: P2)

Several cards are in flight and one looks wrong. The operator narrows the feed to that card alone and reads its recent history — the stages it passed through, the work it did, any warning or error that preceded its current state.

**Why this priority**: Filtering and retained history make the feed diagnostically useful rather than merely reassuring, but the feed delivers its primary value (Stories 1 and 2) without them.

**Independent Test**: With two or more cards active, apply the single-card filter and confirm only that card's entries remain and that the retained history reaches far enough back to cover a full stuck threshold window.

**Acceptance Scenarios**:

1. **Given** entries from several cards are in the feed, **When** the operator filters to one card, **Then** only that card's entries are shown, and clearing the filter restores the full feed.
2. **Given** a card was auto-recovered from a blocked state, **When** the operator reviews that card's feed, **Then** the recovery is present as an entry alongside the block that preceded it.
3. **Given** a card ends in an error or blocked state, **When** the operator reviews that card's feed, **Then** a terminal entry records the outcome and the reason.
4. **Given** the feed has been running long enough to exceed its retention bound, **When** new entries arrive, **Then** the oldest entries are discarded and the dashboard remains responsive.

---

### Edge Cases

- **Many active cards at once.** Entries from all cards interleave in one chronological stream; the per-card filter is how an operator separates them. Retention is a global bound, so a very chatty card can age out a quiet card's entries — acceptable at the concurrency this deployment supports.
- **A browser tab connects late, disconnects, and reconnects.** On each connect the client receives the retained history, so a tab that was closed during a stall still shows the stall entry on reopen. Entries produced while disconnected are recovered from that history rather than replayed individually.
- **A slow or backgrounded browser tab.** The dashboard already drops events for clients that cannot keep up rather than stalling the daemon; that behavior is preserved. A client that misses live entries recovers the current picture on its next reconnect, and dropped delivery must never block or slow the daemon's cycle.
- **The stream drops while the operator is watching.** The feed goes not-live (FR-025, FR-026) rather than merely going still, keeps its existing entries visible, and marks them as possibly stale. When delivery resumes, the on-connect backfill (FR-003) restores anything missed and the indicator returns to live.
- **The daemon dies while a tab stays open.** The stream ends, so the feed reports not-live. An operator is therefore never shown a still, apparently-healthy feed backed by a dead daemon.
- **The stall watchdog is disabled** (its default). No stall entries are produced, and the stuck threshold for the monitoring phases is an hour. The feed-derived quiet marker (FR-028) is what covers this window, and it is the signal a stock install actually relies on.
- **A card is legitimately slow rather than wedged** — a long single tool call with no intermediate output. It is marked quiet, which is a prompt to look rather than a verdict. The card keeps running untouched (FR-031) and clears the moment it emits anything.
- **A stuck condition persists for hours.** The existing cooldown governs how often the condition is re-surfaced, so the feed shows a bounded number of reminders rather than one entry per cycle.
- **Daemon restart.** The feed is in-memory and starts empty after a restart. The dashboard indicates that the feed covers only the current daemon run so an empty feed is not misread as "nothing is happening." A card restored straight into a monitoring phase has no retained entry *and* produces no stage transition for the feed to observe, so quiet detection falls back to its dispatch timestamp (FR-028) — without that fallback the one card this feature exists to surface would be invisible after the restart most likely to have been caused by it.
- **The same action happens twice.** Suppression is keyed on content (FR-022) and the key carries no occurrence counter, so an agent that performs a genuinely identical action twice — the same tool call, on the same file, at the same stage — produces one entry rather than two. SC-003 is satisfied per *distinct* activity, not per action. This is the deliberate cost of FR-022: any key that could tell a real repeat from a re-report would also let a wedged agent's re-reported list scroll, which is the one failure this feature must not have. The untruncated, unsuppressed sequence remains in the existing performer-log panel.
- **A wedged agent whose backend keeps re-reporting its full event list.** Because entries are emitted once per distinct event (FR-022), re-reports produce nothing new and the feed correctly goes quiet. This is the inverse failure the feature must avoid: a wedged agent must never produce a scrolling feed, which would read as healthy progress.
- **The source event list discards its oldest entries while a turn is long-running.** Suppression is keyed on event content rather than on position or count (FR-023), so genuinely new events still appear after a discard.
- **A card ends while entries are in flight.** Entries for a completed card remain in the retained history and remain filterable until they age out; only the per-card suppression bookkeeping is released (FR-024).
- **An entry's source data is malformed or missing a field.** The entry is still rendered with whatever attribution is available; a bad event never breaks the feed or the surrounding dashboard.
- **A source event carries a very large payload** — a tool call containing a whole file, or a long stack trace. The line is truncated at ingest (FR-035) so neither memory nor layout is affected. The untruncated text remains available in the existing performer-log panel, which this feature does not change.

## Requirements *(mandatory)*

### Functional Requirements

**Activity stream**

- **FR-001**: The dashboard MUST deliver activity entries to connected browsers as a distinct, append-only stream, separate from the existing whole-snapshot state updates.
- **FR-002**: The existing snapshot update behavior MUST be preserved **additively** — same delivery cadence, same client handling, and every pre-existing key present with unchanged type and semantics. Adding a new key is permitted; removing, renaming, or repurposing an existing one is not. Existing dashboard panels MUST continue to function exactly as they do today.
- **FR-003**: A newly connected browser MUST receive the retained activity history on connect, so the feed is populated immediately rather than only from the moment of connection.
- **FR-004**: Delivery of activity entries to a slow or unresponsive browser MUST NOT block, delay, or fail the daemon's cycle.

**Entry content**

- **FR-005**: Each activity entry MUST carry a timestamp, the identity of the card it concerns (including the card's issue number and title where available), the stage or persona responsible, an activity type, and a short human-readable line describing what happened.
- **FR-035**: An entry's human-readable line MUST be truncated to a fixed maximum length at the moment the entry is created, defaulting to approximately 200 characters, and only the truncated form is retained. A truncated line MUST be visibly marked as truncated.
- **FR-036**: Every other stored field MUST likewise be bounded in length, so that maximum entry count multiplied by maximum entry size yields the hard memory ceiling required by FR-034.
- **FR-037**: Truncation MUST preserve the beginning of the line, so the part identifying what happened survives.
- **FR-006**: Activity types MUST cover, at minimum: stage entered or changed, agent progress, tool use, cost or token update, quiet warning, stall warning, stuck warning, auto-recovery, and terminal error or blocked outcome.
- **FR-007**: Entries MUST be presented newest-first and MUST be ordered consistently — two entries with the same timestamp MUST have a stable, deterministic relative order.
- **FR-008**: An entry MUST render even when some attribution is unavailable, degrading to whatever fields are known rather than being dropped or breaking the feed.
- **FR-022**: Each activity entry MUST carry a content-derived identity, and an underlying agent event MUST produce at most one feed entry no matter how many times it is observed. Repeatedly observing the same event MUST NOT advance the feed.
- **FR-023**: Duplicate suppression MUST remain correct when a source re-reports its entire accumulated event list on every observation, and when that list's own bound discards its oldest entries. A genuinely new event MUST still produce an entry after such a discard.
- **FR-024**: The memory used for duplicate suppression MUST be bounded per card and MUST be released when the card is no longer active.

**Stall and stuck visibility**

- **FR-009**: Stuck-card detection MUST be evaluated on its configured threshold **regardless of whether any notification channel is configured**, and MUST produce an activity entry whenever it fires.
- **FR-010**: When the stall watchdog trips, it MUST produce an activity entry naming the card, the stage, and the elapsed duration without progress.
- **FR-011**: When a notification channel *is* configured, external dispatch MUST continue unchanged. The activity entry and the external notification are independent outputs of the same detection; neither suppresses the other.
- **FR-012**: Auto-recovery of a blocked card MUST produce an activity entry, and MUST do so regardless of whether a notification channel is configured.
- **FR-013**: Repeated firing of a persisting stall or stuck condition MUST be rate-limited in the feed, reusing the existing per-condition cooldown and deduplication rather than introducing a second, conflicting policy.
- **FR-014**: Stall, stuck, and error entries MUST be visually distinct from routine progress entries.

**Quiet-card detection**

- **FR-028**: The feed MUST mark an active card as *quiet* when nothing has been heard from it for longer than a quiet threshold, so an operator gets a signal well before the stuck threshold on a stock install. Silence is measured from the card's newest activity entry, or — when the card has no retained entry — from the dispatch timestamp the dashboard already reports for that card. A card that has produced no entries at all MUST therefore still be markable as quiet.
- **FR-029**: The quiet threshold MUST be configurable, MUST default to a value on the order of minutes, and MUST be shorter than the smallest stuck threshold that can apply to a card.
- **FR-030**: Quiet detection MUST be derived solely from timestamps the dashboard already receives — retained entry timestamps plus the per-card dispatch timestamp already present in the snapshot. It MUST NOT introduce new detection in the daemon, MUST NOT add a snapshot field for this purpose, and MUST NOT depend on the stall watchdog being enabled.
- **FR-031**: Quiet is an observation, not an intervention. Marking a card quiet MUST NOT terminate a turn, consume retry budget, change a card's phase, or alter the behavior of the stall watchdog or stuck detection in any way.
- **FR-032**: A card MUST return to its normal state as soon as it produces a new entry, and MUST produce at most one quiet entry per quiet episode so that a persistently quiet card does not repeatedly advance the feed.
- **FR-033**: A quiet entry MUST NOT be styled or counted as progress; the feed's "work is happening" cue MUST come only from genuine agent activity.

**Feed presentation**

- **FR-015**: The dashboard MUST present the feed as a scrolling, chronological panel that updates live without a manual page refresh.
- **FR-016**: Operators MUST be able to filter the feed to a single card and to clear that filter.
- **FR-017**: The feed MUST reuse the dashboard's existing activity-type styling so entry types are visually consistent with the panels that already render them.
- **FR-018**: The feed MUST indicate that its history covers only the current daemon run.
- **FR-025**: The feed MUST display a liveness indicator showing whether it is currently receiving updates, so a still feed is never ambiguous between "nothing is happening" and "I am no longer being told what is happening."
- **FR-026**: The indicator MUST change to a visibly not-live state when the stream closes, errors, or falls silent past a small multiple of the interval at which the transport is expected to signal liveness — wide enough that one lost signal is not mistaken for a dead stream — and MUST return to live when delivery resumes.
- **FR-027**: While not live, the feed MUST continue to show its retained entries rather than clearing them, and MUST make clear that what is shown may be out of date.

**Retention**

- **FR-019**: Retained activity history MUST be bounded by a fixed maximum entry count and held in memory only; no new persistence is introduced.
- **FR-020**: The cap MUST default to approximately 2000 entries, sized so that history spanning the longest applicable stuck threshold survives at the **default** card concurrency. The bound is a count, not a duration — retention MUST NOT be governed by entry age — and it MUST NOT scale with concurrency: it is one fixed memory budget shared across all cards.
- **FR-021**: When the cap is reached, the oldest entries MUST be discarded first, and neither the daemon nor the dashboard may degrade as a result.
- **FR-034**: Memory use MUST have a hard ceiling that holds regardless of activity volume or run duration, so a long-running daemon cannot grow without bound through the feed. The ceiling follows from the entry-count cap combined with the per-entry size bound (FR-035, FR-036); neither alone is sufficient.

### Key Entities

- **Activity Entry**: One thing that happened, at one moment, attributable to one card. Carries a timestamp, card identity (id, issue number, title), the responsible stage or persona, an activity type, a short human-readable line, a stable ordering key, and a content-derived identity used to guarantee it is emitted only once. Immutable once created.
- **Seen-Event Set**: The bounded, per-card record of which source events have already produced an entry. Makes emission idempotent against re-reported event lists, and is discarded when the card goes inactive.
- **Activity Type**: The classification that drives both meaning and styling — stage change, progress, tool use, cost, quiet, stall, stuck, recovered, error/blocked.
- **Quiet State**: A per-card, feed-derived observation that nothing has been heard from a card for longer than the quiet threshold. Computed from the card's newest retained entry timestamp or, absent any entry, its reported dispatch timestamp; cleared by any new entry; carrying no authority over the card's execution.
- **Activity History**: The bounded, in-memory, newest-first collection of entries for the current daemon run. Serves both the live stream and the on-connect backfill.
- **Detection Signal**: A stall or stuck condition observed by the daemon. Produces an activity entry always, and an external notification only when a channel is configured — the separation that makes the feed independent of notification setup.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With no notification channel configured, an operator watching the dashboard learns that a card has gone stuck within one detection threshold of the condition arising, without consulting logs, containers, or any external service.
- **SC-002**: An operator can determine whether the system is making progress within 10 seconds of looking at the dashboard, using only the feed.
- **SC-003**: While an agent is actively working, the feed advances at least once per **distinct** agent activity, so a working system is visibly distinguishable from an idle one at a glance. "Distinct" is load-bearing: content-identical repeats are suppressed by FR-022, for the reason given in the Edge Cases.
- **SC-004**: **Agent-activity entries** — progress, tool use, thinking, cost — reach an open dashboard within 3 seconds of the **coordinare observing** the underlying event. The coordinare learns of agent activity only when it polls the performer, so the poll interval bounds how fresh any UI can be; this criterion measures the latency this feature owns — observation to browser — and not the agent-to-observation gap it cannot influence. It is therefore measured from the moment the event batch is handed to the coordinare, not from the moment the agent acted, and those entries MUST be emitted at that handoff rather than waiting for the surrounding cycle to finish. The point-in-time signals (stall, stuck, recovered, error) are emitted at their decision sites and fall inside the same budget.
- **SC-004a**: **Stage-change entries are cycle-granular by design and outside SC-004's budget.** Stage transitions are written by many different nodes; the feed observes them where all of those converge — the dashboard's session watcher — which sees state at cycle-writeback granularity. Emitting them at the handoff instead would require a push at every transition site across the graph: a large diff for a signal whose own resolution is already one cycle. A stage-change entry MUST appear within one cycle of the transition, and that is the whole requirement.
- **SC-005**: At the **default** card concurrency, with the retention cap at its default, history covering the longest stuck-threshold window is still present, so the entry that explains a card's current state is available when the operator goes looking for it. At higher concurrency the cap is a shared global budget: depth degrades, oldest entries go first, and a chatty card can age out a quiet one. The memory ceiling (SC-013) is the invariant; history depth is what gives.
- **SC-006**: A persisting stuck condition produces a bounded number of feed entries governed by the existing cooldown, not one per cycle.
- **SC-007**: All existing dashboard behavior — snapshot updates, the Active Performers tile, the performer-log panel, cycle history — is unchanged, evidenced by the existing dashboard test suite passing without modification.
- **SC-008**: The stall-and-stuck-to-feed path and the activity stream are covered by automated tests, including the zero-notification-channel case.
- **SC-009**: A wedged agent whose backend re-reports an unchanged event list produces zero new feed entries for as long as it stays wedged, so a stuck system never presents as a moving feed.
- **SC-010**: When the stream is interrupted, an operator can tell within 45 seconds — under three keepalive intervals — that the feed is no longer live, so a stale feed is never mistaken for an idle system. The budget is deliberately wider than one interval: a single dropped keepalive must not mark a healthy stream dead, or the indicator becomes noise on precisely the flaky connections it exists to describe.
- **SC-011**: On a stock install with no configuration changes and no notification channel, an operator learns that a card has gone quiet within minutes of it going quiet — not at the 60-minute stuck threshold. This is the measure that the original feedback is answered.
- **SC-012**: Enabling quiet detection changes no card's outcome: the same runs produce the same phases, retry counts, and terminal results as before the feature.
- **SC-013**: Feed memory stays under a fixed ceiling derived from the entry cap and the per-entry size bound, and that ceiling holds across an extended run at maximum card concurrency with agents emitting oversized payloads.

## Assumptions

1. **Single-host, single-process.** The feed is in-memory and per-process, consistent with the rest of the daemon's state model. No cross-process fan-out and no persistence across restarts.
2. **Retention default and its derivation.** ~2000 entries is a **memory budget**, not a derived history depth. It comes from the ceiling it has to hold: 2000 × ~200 characters is under 1 MB, which is the most this feature may cost a long-running daemon. It is deliberately *not* derived from 20 × 100 — the per-card 100-entry source cap bounds what is in flight at one instant, whereas the feed accumulates every distinct event over time, so one card can produce far more than 100 entries across a 60-minute phase. What 2000 actually buys, stated as a rate rather than a vague "realistic volume": at the default 30-second poll interval a 60-minute monitoring phase is ~120 polls, so at the default concurrency of 1 the whole window survives as long as a card averages no more than **~16 new distinct events per poll** (2000 ÷ 120). Above that rate, or at higher concurrency where the budget is shared, the oldest end of the window is trimmed first. That trade is accepted — the ceiling is the hard requirement, depth is not.
3. **Existing thresholds are reused.** Stuck detection keeps its current default (30 minutes, with 60-minute per-phase overrides for the monitoring phases) and its current cooldown. The stall watchdog keeps its current opt-in default of disabled. This feature changes *where the signals go*, not *when they fire*.
4. **The stock install is the case that must work.** The stall watchdog is disabled by default and the stuck threshold for the monitoring phases is 60 minutes, so neither existing detection gives a timely signal out of the box. The feed-derived quiet marker (FR-028) is therefore the default-configuration answer to the reported feedback; the stuck entry (FR-009) and stall entry (FR-010) are the slower, configurable backstops behind it.
5. **No authentication or per-operator state.** The feed is visible to anyone who can already reach the dashboard, and the card filter is a client-side view preference, not persisted server state.
6. **Sources are existing data.** Entries are derived from data the daemon already produces — agent events, stage transitions, the stall watchdog, and the stuck and auto-recovery detections. No new instrumentation is added to the agents.

## Dependencies

- The existing dashboard and its live-update transport, including the `active_sessions` watcher. The watcher observes state at **cycle-writeback** granularity rather than truly mid-cycle: the per-session fanout invokes the graph on a copy, so in-flight mutations are not visible until the invocation returns (research R1). That is why agent activity is emitted at its observation site instead of derived here, and why stage changes — which are cycle-granular anyway — still are.
- The existing per-card agent event stream, stage/phase state, and performer metrics.
- The existing stall watchdog in the performer monitor.
- The existing stuck-card detection in the daemon cycle and auto-recovery detection in the board check.
- The existing notification event types and their deduplication and cooldown behavior.

## Out of Scope

- **Making notification channels genuinely optional at runtime** — ensuring coordinare starts and runs cleanly with zero channels configured is **spec 139**. This spec decouples stall/stuck *detection* from notification *delivery* so the signals reach the UI; 139 owns making a channel-free setup first-class end to end.
- **Example-config secret cleanup** — owned by **spec 132** (#180).
- Persisting activity history across daemon restarts, or exporting it to any external store.
- Replacing, relocating, or restyling the existing Active Performers tile, performer-log panel, or cycle-history panel.
- New agent-side instrumentation or changes to what the agents emit.
- Alerting, escalation, or acknowledgement workflows built on top of the feed.
