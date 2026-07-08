# BLOCKED-Card Auto-Recovery + QA Visual-Capture Resilience (spec 129)

## What it does

**US1 — auto-recovery:** each cycle, before skipping BLOCKED cards, coordinare
re-evaluates whether the block has cleared and moves recovered cards to the
correct stage. Two block reasons are handled:

- **Stale review** (reuses spec-128): a card BLOCKED behind a human "Changes
  Requested" whose feedback is now addressed (threads resolved / body-only +
  new commits) is moved to IN_REVIEW.
- **Env-blocked** (129a): a card carrying the spec-095 `env_blocked` marker is
  moved back to its pre-BLOCKED working column when the symphony's env-cache is
  healthy again (`cache_dir_ready` & not `runtime_health_failed` &
  `last_bootstrap_succeeded`). An env block forces a cache regen; when that
  regen succeeds the card auto-recovers. Env causes not reflected in the cache
  (e.g. an unreachable assessor backend) leave it blocked (never a false
  recovery). **This is what resumes a US2 capture-tooling HOLD once the tooling
  returns.**

Each recovery emits one `card_auto_recovered` notification. A card blocked for
several reasons only recovers when EVERY detected reason has cleared; a genuine
unresolved human verdict never clears (human-gate safety). Anti-thrash: one
attempt per card per daemon run. Fully fail-safe (a per-card error never breaks
the cycle).

**Default-OFF.** Enable with `COORDINARE_BLOCKED_RECOVERY=1` after live
validation (spec-090 autonomy-feature convention).

**US2 — QA visual-capture resilience:** when QA can't do visual capture because
the tooling/runtime is unavailable (not an app failure), the verdict routes to
a recoverable env-block (HOLD) instead of a hard bounce — so US1's env-recovery
gatherer (129a) can resume it when tooling returns. Never a QA false-pass (spec-120
floor intact): a limited-pass that waives required visual evidence is a
deliberately-deferred, config-gated option, not the default.

## Follow-ups (this increment's scope limits)

- US1 CI-red→green and clarification-answered gatherers remain deferred (no
  authoritative per-card CI-block marker; ambiguous "answered" detection). The
  pure evaluator (`services/blocked_recovery.py`) already supports all four
  reasons — only the CI/clarification signal-gathering is outstanding.
- Anti-thrash marker is in-memory (per run); persist it if cross-restart dedup
  becomes necessary.
- US2 limited-pass advance (FR-011) is deferred (false-pass risk); HOLD is the
  safe default.
