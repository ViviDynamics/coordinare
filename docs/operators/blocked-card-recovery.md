# BLOCKED-Card Auto-Recovery + QA Visual-Capture Resilience (spec 129)

## What it does

**US1 — auto-recovery:** each cycle, before skipping BLOCKED cards, coordinare
re-evaluates whether the block has cleared and moves recovered cards to the
correct stage. This increment handles the **stale-review** case (reuses
spec-128): a card BLOCKED behind a human "Changes Requested" whose feedback is
now addressed (threads resolved / body-only + new commits) is moved to
IN_REVIEW with one `card_auto_recovered` notification. Never clears a genuine
unresolved human verdict (human-gate safety). Anti-thrash: one attempt per card
per daemon run. Fully fail-safe.

**Default-OFF.** Enable with `COORDINARE_BLOCKED_RECOVERY=1` after live
validation (spec-090 autonomy-feature convention).

**US2 — QA visual-capture resilience:** when QA can't do visual capture because
the tooling/runtime is unavailable (not an app failure), the verdict routes to
a recoverable env-block (HOLD) instead of a hard bounce — so US1's env-recovery
(follow-up) can resume it when tooling returns. Never a QA false-pass (spec-120
floor intact): a limited-pass that waives required visual evidence is a
deliberately-deferred, config-gated option, not the default.

## Follow-ups (this increment's scope limits)

- US1 signal-gatherers beyond stale-review (env-recovered / CI red→green /
  clarification-answered) are the next increment; the pure evaluator
  (`services/blocked_recovery.py`) already supports all four reasons.
- Anti-thrash marker is in-memory (per run); persist it if cross-restart dedup
  becomes necessary.
- US2 limited-pass advance (FR-011) is deferred (false-pass risk); HOLD is the
  safe default.
