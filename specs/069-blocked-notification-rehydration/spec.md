# Feature Specification: Block-Notification Dedup on State Rehydration

**Feature Branch**: `069-blocked-notification-rehydration`
**Created**: 2026-05-22
**Status**: Draft
**Input**: Operator observed that within ~2 minutes of restarting coordinare, card #70 produced a Slack `🚫 blocked: needs input` notification and a fresh GitHub reminder comment, even though the performer had just been re-dispatched and was actively running. The "blocked" state was a stale artifact of the rehydrated state snapshot, not a real condition.

## Problem

When coordinare restarts and rehydrates a state snapshot whose top-level `phase == "blocked"` (or whose active session is in `phase == "blocked"`), the orchestration loop re-enters `handle_blocked` and `notify` against the rehydrated state. Three independent defects compound:

1. **Spurious Slack post.** `notify.py` emits `EventType.card_blocked` whenever `phase == "blocked"`. The dedup key is `f"{event_type}:{card_id}:{status}:{performer_stage}"` — it does not encode the *content* of `open_questions` or any `last_blocked_notified_at` watermark. After a restart the dedup cache is empty, so the same logical "blocked" state re-fires as a fresh notification.

2. **Empty-question fallback masks the real situation.** When `notify` runs and `open_questions` is empty (which happens transiently between rehydration and the `handle_blocked` reassess pass that regenerates questions), `notify.py:91` falls back to the literal string `"needs input"`. The operator sees a generic "blocked: needs input" with no actionable detail, which is indistinguishable from a real blocked card.

3. **Reminder-comment gate bypassed on rehydration.** `handle_blocked.py:142-159` reposts the GitHub reminder comment whenever `last_blocked_notified_at` is `None`. Session-level `last_blocked_notified_at` is not persisted/rehydrated symmetrically with top-level (snapshot for card #70 showed top-level `2026-05-21T21:32:04Z` but session-level `None`), so on restart the gate sees `None` and reposts immediately.

Concretely, for card #70 on 2026-05-22: coordinare restarted at 12:02 UTC, dispatched the implementer at 12:03, and emitted a false `🚫 blocked: needs input` Slack post at 12:04 — while Hermes was still actively working. The persisted open question (`"Performer (implementing) encountered an error: malformed_output"`) was dropped before notify ran, so even the dedup key was not influenced by the prior reason.

## Goals

- After a state rehydration, coordinare MUST NOT re-emit a `card_blocked` notification for a session whose blocked state has already been notified (as recorded by `last_blocked_notified_at`).
- After a rehydration, coordinare MUST NOT repost the GitHub reminder comment unless the configured reminder interval (`blocked_reminder_hours`, default 24h) has actually elapsed since the *persisted* `last_blocked_notified_at`.
- When a card is being concurrently re-dispatched (a fresh performer session is active and not yet terminal), coordinare MUST NOT emit a `card_blocked` notification against the prior blocked state — the fresh dispatch supersedes it.
- `notify.py` MUST NOT emit a `card_blocked` event whose summary is the generic `"needs input"` fallback. If `open_questions` is empty at notify-time, the notification is suppressed (the state is in flux; either real questions will arrive or the card will transition out of blocked).

## Non-Goals

- Changing the semantics of `phase == "blocked"` itself or any blocked-handling business logic outside notification dedup and rehydration symmetry.
- Auditing or replaying historical blocked notifications that fired before this fix.
- Fixing the upstream `malformed_output` classification that originally seeded the stale blocked state (tracked separately — recent Hermes code already excludes `implementer` from `_JSON_ONLY_ROLES`).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Restart with a real blocked card does not re-spam (Priority: P1)

**Given** a state snapshot in which card X is genuinely blocked, with `last_blocked_notified_at` set to a timestamp within the past `blocked_reminder_hours` window, **When** coordinare restarts and rehydrates that snapshot, **Then** no new Slack `card_blocked` event is emitted for card X and no new GitHub reminder comment is posted until the reminder interval elapses.

**Independent Test**: Seed `coordinare.state.json` with `phase=blocked`, populated `open_questions`, and `last_blocked_notified_at = now - 1h`. Restart coordinare. Assert: zero new Slack dispatches for `card_blocked` against card X; zero new GitHub comments on card X's issue.

### User Story 2 — Restart while a fresh performer is running suppresses stale blocked (Priority: P1)

**Given** a state snapshot whose session for card X is `phase=blocked` AND a fresh performer session for card X has been dispatched after restart, **When** the orchestration loop next runs `notify`, **Then** no `card_blocked` event is emitted for the stale blocked state — only the new dispatch notification fires.

**Independent Test**: Reproduce the 2026-05-22 12:02 sequence in an integration harness. Assert: exactly one Slack event (`card_dispatched`) within the first 5 minutes after restart; no `card_blocked` event.

### User Story 3 — Empty open_questions never produces a "needs input" Slack post (Priority: P2)

**Given** orchestration state with `phase=blocked` and `open_questions == []`, **When** `notify` runs, **Then** no Slack notification is emitted (the in-flight reassess pass owns the resolution path).

**Independent Test**: Unit-test `notify.py` directly: set `phase=blocked`, `open_questions=[]`, populated card. Assert: `notification_service.dispatch` was not called.

## Functional Requirements *(mandatory)*

- **FR-001**: Session-level `last_blocked_notified_at` MUST be serialized into the persisted snapshot and rehydrated symmetrically with top-level `last_blocked_notified_at`.
- **FR-002**: `handle_blocked` MUST NOT repost the reminder comment when the rehydrated `last_blocked_notified_at` is within the configured reminder window, regardless of whether the value came from a fresh in-memory write or from a rehydrated snapshot.
- **FR-003**: `notify` MUST skip dispatching `card_blocked` events when `open_questions` is empty.
- **FR-004**: `notify` MUST skip dispatching `card_blocked` events when (a) the rehydrated `last_blocked_notified_at` is non-null and the dedup cache is empty (i.e., we just restarted), AND (b) no new question has been added since that timestamp.
- **FR-005**: When a fresh performer session is active (`active_sessions[card_id]` with a non-terminal `phase` such as `dispatching`, `monitoring_performer`, or `monitoring_agent`) for the same card, `notify` MUST NOT emit `card_blocked` for that card on the same tick, regardless of stale top-level `phase`.
- **FR-006**: The notification dedup key for `card_blocked` MUST include a content hash of `open_questions` so identical re-emits collapse and content changes are visible.

## Affected Files (preliminary)

- `src/coordinare/graph/nodes/notify.py` — suppress empty-question blocked events; extend dedup key with question-content hash; consult `last_blocked_notified_at` + active_session phase before emitting `card_blocked`.
- `src/coordinare/graph/nodes/handle_blocked.py` — read session-level `last_blocked_notified_at` symmetrically; do not collapse `None` to "post now" when a session-level value exists.
- `src/coordinare/state_store.py` — ensure session-level `last_blocked_notified_at` is serialized to and rehydrated from the snapshot.
- `src/coordinare/session.py` — confirm session schema carries `last_blocked_notified_at` across `to_dict`/`from_dict`.
- `src/coordinare/daemon.py` — confirm rehydration path copies the session-level timestamp.
- `tests/unit/graph/nodes/test_notify.py` — new cases for User Stories 2 & 3 and FR-006.
- `tests/unit/graph/nodes/test_handle_blocked.py` — new cases for FR-001/FR-002.
- `tests/unit/test_state_store.py` (or equivalent) — round-trip session-level `last_blocked_notified_at`.

## Risks & Edge Cases

- **Genuinely re-blocked after restart.** If a card's open_questions content materially changes after restart (e.g., a new clarifying question was added by an assessor), the content-hash dedup MUST allow that new event through. Tests must cover both "same questions" (suppress) and "new questions" (emit).
- **Reminder cadence drift.** Symmetric rehydration of `last_blocked_notified_at` means a card that was about to hit the 24h reminder right before a restart will hit it on schedule, not be reset. That is intended.
- **Top-level vs session-level divergence.** Two timestamps for the same concept invites drift. The fix should treat session-level as authoritative for per-card decisions and either remove the top-level field or keep it strictly as a UI/display mirror.
- **Active-session check race.** Between `handle_blocked` and `notify` in the same tick the active session could flip. The check should be done at the point of notify, against the same state snapshot.

## Out of Scope

- Redesigning the notification dispatcher or moving off the existing `NotificationEvent` model.
- Replacing `phase == "blocked"` with a richer enum.
- Backfilling Slack history.
