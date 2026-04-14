# 042 — Review Workflow, PR Linkage & Documentation Trail

## Summary

Comprehensive review workflow improvements, PR-to-issue linkage, per-stage notifications, architecture documentation trail, trusted bot reviewers, project wiki maintenance, token refresh, the review loop bug fix, and the new closer role for final thread resolution.

## Motivation

After merging specs 040 (e2e lifecycle fixes) and 041 (operational hardening), continued live testing with a full 7-role lifecycle (assessor → architect → implementer → reviewer → security → qa → tech_writer) revealed workflow gaps and new requirements. Live testing of PR #88 (favicon) further surfaced that branch ruleset enforcement of "all conversations resolved" stranded merges when the substantive reviewer was bypassed in relay flows — driving the closer addition.

## Changes

### Review Loop Fix
Old reviews re-triggered dispatch when `lifecycle_completed_at` was cleared. Added `processed_review_ids` (set of review node IDs) to state — reviews already processed are skipped. IDs are marked processed in `classify_human_feedback` (crash-safe: after relay commits to state, not before).

### Trusted Bot Reviewers
`TRUSTED_BOT` reviewer type — Copilot's review feedback triggers the performer lifecycle. Only HUMAN reviews can trigger merge (TRUSTED_BOT cannot approve).

### Per-Stage Notifications
Each lifecycle stage gets its own Slack/email notification. "Ready for human review" notification on lifecycle completion. Human reviewers requested via GitHub API.

### PR-to-Issue Linkage
PR body starts with "Closes #N". Existing PRs updated on re-dispatch via PATCH.

### Architecture Documentation Trail
Each card gets a structured folder: `docs/cards/{issue}-{short_slug}/`
- assessment.md, plan.md, tasks.md, security.md, qa.md
- All personas output content only — "Do NOT commit files yourself"

### Tech Writer & Project Wiki
The tech writer maintains `docs/wiki/` — a persistent, evolving knowledge base:
- README.md, setup.md, testing.md, architecture.md, history.md
- Can break long pages into subdirectories
- Cleans up stray files, ensures docs/cards/ consistency
- Updates CHANGELOG.md and inline documentation

### Token Refresh
Coordinare sends a fresh GitHub App token in every status check payload. Performer updates its stored token on receipt — prevents 401 errors from expired tokens during long lifecycle runs.

### Reviewer Improvements
Binary reviews (no suggestions), thread resolution on approval, comment normalization (strings → dicts).

### Closer Role (final thread-resolution sweep)
New eighth performer `closer` runs at the end of every lifecycle (including relay flows where the substantive reviewer would otherwise be skipped). The closer's job is narrow:

- Verify each open PR review thread was addressed by subsequent commits
- Approve and resolve all threads when prior concerns are addressed
- Request changes with file:line guidance when any concern remains

Distinct from the substantive reviewer:
- Reviewer runs after implementer; deep code review
- Closer runs after every other performer; verification of addressed feedback only — no new style/convention nits

Lifecycle becomes: `assessor → architect → implementer → reviewer → security → qa → tech_writer → closer`.

This eliminates the merge-block scenario where ruleset's `required_review_thread_resolution: true` stranded an otherwise-mergeable PR because the lifecycle's substantive reviewer ran early and the relay routing (e.g., qa-only feedback) skipped it on subsequent passes, leaving Copilot threads unresolved across the lifecycle.

Distinct stage name `closing_review` (not duplicate `reviewing`) so `_advance_stage`'s `sequence.index(current)` lookup advances correctly past the substantive reviewer when the closer runs.

### Resolve-Threads Reliability
`resolve_pr_review_threads` previously checked only the HTTP status code, but GraphQL returns 200 with errors in the response body — so failed mutations were silently counted as successes. Function now detects GraphQL errors and verifies `isResolved=true` came back from each mutation, with logged thread-id/author/outdated context for every failure. Targets all unresolved threads regardless of author (humans, the coordinare bot, Copilot, other bots).

### Dashboard
Shows current performer stage with progress (e.g., "implementing (3/7)").

### Configuration & Resilience
All 7 performers in example config. Fibonacci backoff (1.618x). Higher circuit breaker thresholds.

### Snapshot Serialisation
Snapshot `_build_snapshot` was using `str(card_dict.get("pr_url", ""))`, which returns the literal string `"None"` when the value is explicitly None (because the default `""` only fires on missing keys). After restart, `monitor_pr` would query GitHub with the literal string `"None"` and trip the circuit breaker. Replaced with an explicit `_str_or_none` helper that converts None/empty to None first, then stringifies.

### PR Recovery
When a restart loses `pr_node_id` (bad snapshot, crash mid-lifecycle), `monitor_pr` now walks the issue's `closedByPullRequestsReferences` linkage to find the open PR and rehydrate the card — works reliably because the implementer always includes `Closes #N` in PR bodies.

### Permanent vs Transient Error Classification
`squash_merge` on a branch with protection that doesn't allow the App to push returns `UNPROCESSABLE` — a logical error (won't resolve on retry), not a service health signal. Previously counted as a circuit-breaker failure: 5 retries → breaker trips → every subsequent GitHub call blocks until recovery. Now classified as `PermanentGitHubError`, bypasses the breaker's failure accounting via new `guard(ignore=...)` parameter, and causes `merge_pr` to transition to `blocked` with the actual GitHub error in `open_questions` so a human can act.

### False "merged!" Notifications
`notify` was mapping `phase=merging` to `card_merged` event. But `merging` is the *attempt* phase — `merge_pr` loops back to `merging` on every retry. Every failed retry fired a "✅ merged!" Slack notification. Real success is detected via `commit_summary` (only set by `merge_pr` after `squash_merge` returns successfully) or `card.status=DONE`. Merging-phase retries without success signal are silently suppressed. `commit_summary` is cleared on new-card pickup so the next card's dispatches don't inherit stale success detection.

### Blocked-Card Dispatch Loop
After `merge_pr` correctly transitioned blocked cards, a new loop emerged: `check_board` was reading the bot's own reminder comments as "user answers" to the blocked questions, moving the card to IN_PROGRESS and re-dispatching every ~90s. GitHub records `createdAt` at second precision while local timestamps are sub-second, so the bot's fresh reminder could look "newer than the cutoff". Two fixes: `handle_blocked` now always advances the cutoff timestamp (decoupled from the 24h reminder gate), and `check_board` filters bot-authored comments by login — only humans can supply answers.

### Branch Protection Both-Sides Gotcha
Repositories can have both a modern **Ruleset** (`Settings → Rules → Rulesets`) AND a classic **Branch Protection** rule (`Settings → Branches`) on the same branch. Both must allow the GitHub App to push. Adding the App only to the Ruleset's bypass list (while the classic rule's `Restrict who can push` stays empty) produces the confusing "not authorized to push to this branch" error even though the operator just updated settings. README now documents both systems explicitly.

### Mailgun SMTP Example
`config.example.yaml` previously showed `smtp_host: smtp.example.com` — an unresolvable placeholder. Updated to `smtp.mailgun.org` with the required `postmaster@mail.yourdomain.com` username format and EU endpoint note, matching the confirmed-working production configuration.

## Out of Scope
- DNS crash in monitoring_performer (runtime-level transport error handling)
- Monitor_pr: check unresolved threads when no new reviews
