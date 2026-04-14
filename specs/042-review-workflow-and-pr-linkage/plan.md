# 042 — Review Workflow & PR Linkage — Implementation Plan

## Overview

10 commits across 25 files (~570 lines). Fixes review workflow gaps, adds PR linkage, per-stage notifications, architecture documentation trail, and trusted bot reviewer support.

## Architecture Decisions

### AD-1: Processed review IDs over timestamp-only cutoff
`lifecycle_completed_at` fails when cleared by `classify_human_feedback`. `processed_review_ids` (set of review node IDs) survives cutoff clears and prevents re-dispatch of already-processed reviews. Cleared when a new card is picked up.

### AD-2: Trusted bot reviewers as a separate type
`TRUSTED_BOT` is distinct from `HUMAN` and `BOT`. Configured via `trusted_bot_reviewers` list. Both HUMAN and TRUSTED_BOT reviews are actionable (trigger lifecycle dispatch). Regular BOT reviews are ignored. This keeps the reviewer classification clean without conflating bot accounts with human accounts.

### AD-3: Architecture folder per card
`docs/cards/{issue}-{slug}/` mirrors speckit's folder-per-feature pattern. Each lifecycle role writes its output as a file (assessment.md, plan.md, security.md, qa.md). Roles read previous outputs — architect reads assessment.md, implementer reads plan.md. Everything is committed to the feature branch and visible in the PR.

### AD-4: Binary reviewer
The reviewer produces either blocking issues (approved=false, comments list) or approval (approved=true, no comments). No "suggestions" or "non-blocking" noise. Scope-expanding feedback is the assessor's job, not the reviewer's.

### AD-5: Closer is a distinct role with a distinct stage name
The closing pass needs to run AFTER tech_writer at the end of every lifecycle (including relay flows where `classify_human_feedback` routes to a single performer like qa). Three options were considered:
1. **Reuse the substantive reviewer twice** — rejected: same persona instructions would produce duplicate noise on changes already made by qa/security/tech_writer; also `_advance_stage` uses `sequence.index(current)` which returns the first occurrence, creating an infinite loop on duplicate stage names.
2. **Move thread resolution to the coordinare itself** — rejected: lacks judgment. Resolving an outdated thread when the "fix" was wrong masks problems.
3. **Add a new performer role with a distinct stage name (chosen)** — `closer` role with `closing_review` stage. Distinct stage name keeps `_advance_stage` correct. Distinct persona gives narrow closing-specific instructions: verify prior feedback was addressed, no new style nits, binary verdict.

Lifecycle becomes: `assessor → architect → implementer → reviewer → security → qa → tech_writer → closer`. The closer's verdict either resolves all threads (clearing the merge gate) or surfaces what remains unaddressed for the next implementer pass.

### AD-6: GraphQL errors must not be counted as success
`resolve_pr_review_threads` previously returned `resolved += 1` based purely on `resp.is_success` (HTTP 200), but GraphQL APIs return 200 with errors in the response body. This silently masked failed thread resolutions on PR #88 — reviewer thought it had resolved Copilot's thread, but the GraphQL mutation had errored. Function now requires both: HTTP 200 AND `resolveReviewThread.thread.isResolved == true` AND no `errors[]` in the response body. Failures logged with thread id, author, and outdated flag for diagnosis.

## Files Changed

| File | Changes |
|------|---------|
| `src/coordinare/lifecycle.py` | `closer` role + `closing_review` stage in CANONICAL_ORDER |
| `src/coordinare/graph/nodes/monitor_pr.py` | processed_review_ids filtering, trusted bot support, **PR recovery via issue→PR linkage when pr_node_id is missing/"None"/stale** |
| `src/coordinare/graph/nodes/monitor_performer.py` | Ready-for-review notification, human reviewer request |
| `src/coordinare/graph/nodes/notify.py` | Stage in dedup key, monitoring_performer phase mapping, **real-merge detection via commit_summary, suppress merging-phase retries** |
| `src/coordinare/graph/nodes/check_board.py` | Clear processed_review_ids + **commit_summary** on new card, **filter bot-authored comments from answer detection** |
| `src/coordinare/graph/nodes/dispatch_performer.py` | `closing_review` → `closer` mapping |
| `src/coordinare/graph/nodes/handle_blocked.py` | ✅ Closer role label, **always advance cutoff timestamp to break dispatch loop** |
| `src/coordinare/graph/nodes/merge_pr.py` | **Permanent GitHub error → blocked (with actual error message), transient errors still retry** |
| `src/coordinare/graph/state.py` | processed_review_ids, trusted_bot_reviewers fields |
| `src/coordinare/models/review.py` | TRUSTED_BOT type, updated classify_reviewer |
| `src/coordinare/config.py` | trusted_bot_reviewers, wait_exp_base, closer role + persona |
| `src/coordinare/__main__.py` | Wire trusted_bot_reviewers, wait_exp_base |
| `src/coordinare/daemon.py` | Persist processed_review_ids, **_str_or_none helper (None → JSON null, not literal "None")** |
| `src/coordinare/state_store.py` | processed_review_ids in WorkflowSnapshot |
| `src/coordinare/metrics.py` | closing_review in per-role token counter |
| `src/coordinare/resilience.py` | **`guard(ignore=...)` parameter — skip failure accounting for app-layer errors** |
| `src/coordinare/services/github.py` | request_reviewers, link_to_project, **find_pr_for_issue recovery method, GraphQL error classification (UNPROCESSABLE/FORBIDDEN/NOT_FOUND/UNAUTHORIZED → Permanent), _guarded_execute ignores PermanentGitHubError, module-level logger (replaced phantom self._logger)** |
| `src/coordinare/services/persona_service.py` | Assessor/architect/reviewer persona updates + closer persona |
| `src/coordinare/dashboard.py` | performer_stage display |
| `agent/performer/src/performer/main.py` | Doc folder commits, binary reviewer, closing_review handling, error details |
| `agent/performer/src/performer/models.py` | issue_number, issue_url on Score |
| `agent/performer/src/performer/github.py` | PR body linkage, update existing PR, resolve threads with GraphQL error detection (author-agnostic, surfaces silent failures) |
| `config.example.yaml` | All 8 performers (closer added), trusted bots, App auth, **Mailgun SMTP example** |
| `README.md` | **Branch protection authorisation guidance (Ruleset + classic rule both required)** |
| `specs/003-state-persistence/contracts/workflow-snapshot.schema.json` | processed_review_ids |
