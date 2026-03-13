# Feature Specification: Performer Hardening

**Feature Branch**: `013-performer-hardening`
**Created**: 2026-03-12
**Status**: In Progress

## Overview

After the initial performer implementation (012) was exercised in a real environment, several integration gaps surfaced:

1. The AI coding agent subprocess had no git credentials and could not push to GitHub.
2. A failing performer (infrastructure error) was indistinguishable from a blocked performer (agent question) — both routed to the `blocked` phase requiring human intervention.
3. Transient network failures (transport errors, unreachable health checks) triggered permanent `blocked` states rather than retrying.
4. Push failures with `--force-with-lease` when no remote-tracking ref exists caused `(stale info)` rejections on coordinare-managed branches.
5. A second dispatch to a branch that already had a PR raised `GitHubAPIError(422)` instead of returning the existing PR.
6. AI agent prompts incorrectly instructed the agent to push and open a PR, which is the performer's job.

This spec captures the hardening work done to address all of these gaps.

## Clarifications

### Session 2026-03-12

- Q: Should git credentials be written to disk (`.git/config`) or passed transiently? → A: In-memory only via `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0` environment variables. Combined with short-lived GitHub tokens (already in use), credentials are never persisted after the subprocess exits.
- Q: Should infrastructure errors retry silently or immediately alert operators? → A: Retry up to 3 times at 90-second intervals before notifying. A single network blip should not page anyone.
- Q: Should `TransportError` (network failure to performer) block a card permanently? → A: No — transport errors are transient. Route through `system_error` retry logic. Only `PermanentGitHubError` (revoked tokens, 404s) should block immediately.
- Q: `--force-with-lease` vs `--force` for coordinare-managed branches? → A: `--force` is correct. `--force-with-lease` requires a remote-tracking ref (named remote) to evaluate the lease against; pushing directly to a URL has no such ref, so git rejects with `(stale info)`. Coordinare-managed branches (`coordinare/<id>/<slug>`) are exclusively owned by coordinare — no human pushes to them — so `--force` is safe.

## User Scenarios & Testing

### User Story 1 — Secure Credential Passing to AI Subprocess (Priority: P1)

The AI coding agent subprocess (Claude Code, opencode, Codex) needs git credentials to push commits. Credentials must not persist on disk after the performance ends.

**Acceptance Scenarios**:

1. **Given** a performer has cloned a repository, **When** the AI backend subprocess is launched, **Then** git credentials are available via environment variables and the subprocess can push without prompting for a password.
2. **Given** a performance completes or fails, **When** the subprocess exits, **Then** no credential files remain on disk (credentials existed only in subprocess memory).
3. **Given** a git operation fails with an auth error, **When** the error is surfaced in logs or exceptions, **Then** the Authorization header value is redacted and the token is never visible.

---

### User Story 2 — System Error Retry Loop (Priority: P1)

Infrastructure errors (performer crash, network blip, git push failure) should retry automatically without requiring human intervention, escalating only after repeated failures.

**Acceptance Scenarios**:

1. **Given** a performer returns `status="error"`, **When** coordinare processes the result, **Then** it enters the `system_error` phase rather than `blocked`, and retries dispatch after 90 seconds.
2. **Given** 3 consecutive system errors on the same card, **When** the retry limit is reached, **Then** coordinare sends a `performer_error` notification to the operator and moves the card to `BLOCKED` without posting a question comment on the GitHub issue.
3. **Given** a transport error (network timeout) during dispatch or status polling, **When** coordinare handles it, **Then** it routes to `system_error` for retry rather than immediately blocking the card.
4. **Given** a performer health check raises an exception (`unreachable`), **When** coordinare processes it, **Then** it stays `idle` for the next cycle rather than blocking the card.

---

### User Story 3 — Resilient Push and PR Creation (Priority: P2)

Push and PR creation should handle the common cases of an existing branch or existing PR gracefully rather than failing.

**Acceptance Scenarios**:

1. **Given** a branch already exists on the remote, **When** the performer pushes, **Then** it succeeds with `--force` (coordinare-managed branches are exclusively coordinare-owned).
2. **Given** a PR already exists for the branch, **When** `create_pull_request` is called, **Then** it returns the existing PR's `(html_url, node_id)` rather than raising an error.
