# Research: Post-PR Workflow Bug Fixes

**Feature**: 017-fix-post-pr-workflow
**Date**: 2026-03-16

## Summary

No external research required. All decisions are grounded in direct code inspection of the affected files and confirmed from production issues #71 and #77 on ViviDynamics/website.

---

## Decision 1: Move card to IN_REVIEW on `pr_opened`

**Decision**: Call `github.move_card(card_id, "IN_REVIEW")` in the `pr_opened` handler in `monitor_agent.py`.

**Rationale**: The card status was already being set to `"IN_REVIEW"` in memory (line 102 of current code) but the GitHub board was never updated. The `session_expired` and `blocked` handlers both call `github.move_card()` consistently — the `pr_opened` handler simply missed this call. Confirmed from issue #71 where repeated dispatches triggered GitHub 422 errors.

**Alternatives considered**:
- Relying on `check_board` to detect `IN_REVIEW` status via next poll: Rejected — the next poll sees IN_PROGRESS (GitHub hasn't changed), so it routes back to `monitor_agent` instead of `monitoring_pr`.

---

## Decision 2: `session_expired` with existing PR → route to `monitoring_pr`

**Decision**: Check `card.get("pr_node_id")` in the `session_expired` handler. If set, transition to `monitoring_pr` instead of resetting to TODO.

**Rationale**: A non-empty `pr_node_id` on the card means the PR was successfully opened in a prior cycle. The coordinare has already done the work; the session expiry is a teardown artefact. The correct recovery is to resume monitoring the open PR, not to re-dispatch.

**Alternatives considered**:
- Query GitHub API to confirm PR is still open: Rejected — over-engineering for this fix scope. The existing `monitoring_pr` node handles stale/closed PRs. The guard just needs to prevent unnecessary re-dispatch.

---

## Decision 3: Save `open_questions` to `card_clarifications` on session expiry

**Decision**: Before clearing `open_questions` in the `session_expired` handler, append an entry to `card_clarifications` with `answer=""`.

**Rationale**: The `assess_card` node already embeds all `card_clarifications` (including entries with empty answers) into the performer dispatch payload via `current_card.clarifications`. An empty-answer entry signals to the assessment backend that these questions were asked but never answered, so they should be re-asked.

**Alternatives considered**:
- Keep questions in `open_questions` instead of moving to `card_clarifications`: Rejected — `open_questions` is intended for the current blocked session; `card_clarifications` is the persistent Q&A history across sessions.
- Discard questions on expiry: Rejected — confirmed bug causing repeated question loops in issue #71.

---

## Decision 4: Validate workspace context completeness before dispatch

**Decision**: After workspace setup, validate `repo_url`, `branch`, and `github_token` are non-empty before dispatching. Block card if any are missing. Also broaden the exception catch from `WorkspaceSetupError` to `Exception`.

**Rationale**: `workspace_info.github_token` is populated for subprocess transport but empty for Kubernetes transport (which uses K8s Secrets). Sending an empty token to a subprocess performer causes it to run without auth, prompting the user for credentials. The validation guard catches this before the performer is started.

`workspace_path` is intentionally excluded from the required fields because:
- Kubernetes transport: `path=None` is correct (performer self-clones)
- Subprocess transport: `path` is always set when clone succeeds

**Alternatives considered**:
- Fix `workspace_path` being None for Kubernetes: Rejected — intentional design; Kubernetes performers handle their own workspace.
- Only validate on subprocess transport: Rejected — the validation logic is transport-agnostic and simpler to maintain.

---

## No NEEDS CLARIFICATION markers

All requirements are fully specified. Root causes confirmed from direct code inspection and production issue analysis.
