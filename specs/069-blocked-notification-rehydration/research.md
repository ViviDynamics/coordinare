# Phase 0 Research: Block-Notification Dedup on State Rehydration

All open questions from the spec were resolvable from current code and existing
project conventions. No external best-practice research was needed.

## R1. Which `last_blocked_notified_at` is authoritative?

**Decision**: Session-level (`CardSession.last_blocked_notified_at`) is the
authoritative per-card value. Top-level `state["last_blocked_notified_at"]` is
kept as a derived mirror for the active card only (already written every tick
by `handle_blocked.py:207`), consistent with the pattern established in spec
066 for `current_card` (`_set_current_card` / `_rederive_current_card`).

**Rationale**: With `max_concurrent_cards > 1` the top-level scalar cannot
represent multiple cards' watermarks; per-session storage is the only
multi-card-safe location. `CardSession` already declares the field
(`session.py:59`); only `PersistedSession` was missing it.

**Alternatives considered**:
- *Drop top-level entirely.* Rejected for spec 069 scope — `handle_blocked.py`
  and `check_board` consumers still read it, and re-wiring them is out of
  scope. Tracked as a follow-up cleanup (see Risks in spec).
- *Keep top-level as primary, add session-level for redundancy.* Rejected —
  duplicates the spec-066 mistake; one source of truth.

## R2. Should an empty `open_questions` ever produce a Slack post?

**Decision**: No. When `notify` is invoked with `phase == "blocked"` and
`open_questions == []`, suppress the `card_blocked` event entirely. The
literal `"needs input"` fallback at `notify.py:91` is removed; the only path
to a `card_blocked` Slack message is one or more populated questions.

**Rationale**: An empty list at notify-time is *always* a transient artifact
of rehydration order (between snapshot load and the next `handle_blocked`
reassess pass). The operator gains nothing from a content-free
"needs input" string — and as the 2026-05-22 12:04 incident showed, it
masks the actual situation (Hermes was running, not stuck).

**Alternatives considered**:
- *Keep the fallback but tag it `(stale)`.* Rejected — still emits a
  notification for a non-actionable state.
- *Delay-and-retry inside notify.* Rejected — adds tick coupling; the next
  tick's `handle_blocked` will repopulate questions or transition the card.

## R3. How does `notify` know a fresh dispatch has superseded the stale blocked state?

**Decision**: Before emitting `card_blocked` for a card, `notify` consults
`state.get("active_sessions", {}).get(card_id)`. If a session exists and its
`phase` is one of `{"dispatching", "monitoring_performer", "monitoring_agent"}`
(non-terminal, non-blocked phases), the emission is suppressed for that tick.
Terminal phases (`finalized`, `failed`, `closed`) and the literal `"blocked"`
phase do *not* suppress.

**Rationale**: The spec-066 unification makes `active_sessions` the single
source of truth for per-card phase. A non-terminal session that is not itself
in `"blocked"` necessarily represents a fresh dispatch that postdates any
rehydrated top-level `phase == "blocked"`.

**Alternatives considered**:
- *Compare timestamps.* Rejected — `active_sessions` already encodes the
  decision more directly; no need to introduce timestamp arithmetic.
- *Move the check to `handle_blocked` instead.* Rejected — `handle_blocked`
  legitimately re-runs for legitimately-blocked sessions; the right place to
  gate the *Slack-emission* is in `notify`.

## R4. Dedup key extension — what content hash?

**Decision**: Extend the current key
`f"{event_type.value}:{card_id}:{status}:{performer_stage}"` (notify.py:133)
to
`f"{event_type.value}:{card_id}:{status}:{performer_stage}:{questions_hash}"`
where `questions_hash` is the first 12 hex chars of
`hashlib.sha256("\n".join(open_questions).encode("utf-8")).hexdigest()`
when `event_type == EventType.card_blocked`, and the empty string for all
other event types (preserving existing behavior).

**Rationale**: A short prefix is enough to disambiguate identical-vs-changed
question sets without bloating the key. SHA-256 is already in `hashlib` —
no new dependency. The hash is computed only for blocked events, so other
events pay zero cost.

**Alternatives considered**:
- *Full hash digest.* Rejected — longer keys with no behavioral benefit;
  collision probability at 12 hex chars (48 bits) is far below the per-card
  question-set count.
- *List length only.* Rejected — would let two different single-question
  blocks collide.

## R5. Rehydration suppression — how does `notify` know it just restarted?

**Decision**: The signal is *(a) `last_blocked_notified_at` is non-null on
the per-card session* AND *(b) the in-memory dedup cache has no entry for
the would-be key*. When both hold, suppress emission for this tick and
prime the dedup cache with the current (hash-extended) key so subsequent
ticks behave normally. If the question set legitimately changes after
restart, the hash component of the key differs from the primed entry and
a fresh emission fires — no separate persisted hash is required.

**Rationale**: A clean restart is precisely the case where the dedup cache
is empty but a persisted watermark exists. Priming the cache after the
first suppressed tick prevents repeated re-evaluation on every subsequent
tick.

**Alternatives considered**:
- *Persist the dedup cache to disk.* Rejected — larger surface area, new
  serialization concerns, and the watermark+hash combination already
  reconstructs the needed state.
- *Always re-emit on restart and rely on Slack to dedup.* Rejected — Slack
  does not dedup; the spurious post is exactly what we are fixing.

## R6. Backward compatibility with v1 snapshots

**Decision**: `PersistedSession.last_blocked_notified_at` is added as
`datetime | None = None`. v1 snapshots that lack the field deserialize with
the default, producing the same behavior as today on the first post-upgrade
restart (no watermark → reminder logic uses `handle_blocked`'s existing
`last is None` branch, but now coupled with the active-session check from
R3 to prevent the spurious-restart case).

**Rationale**: Matches the additive-only snapshot evolution pattern used in
spec 066. No migration script needed.

**Alternatives considered**:
- *Bump snapshot version.* Rejected — unnecessary for a purely additive
  optional field with a safe default.
