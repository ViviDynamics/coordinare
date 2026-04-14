# 042 — Review Workflow & PR Linkage — Tasks

## Review Loop Fix

- [x] Track processed_review_ids in CoordinareState TypedDict
- [x] Filter processed review IDs in monitor_pr
- [x] Record dispatched review IDs in classify_human_feedback (crash-safe)
- [x] Persist processed_review_ids in WorkflowSnapshot
- [x] Save/restore processed_review_ids in daemon
- [x] Clear processed_review_ids on new card pickup in check_board
- [x] Update workflow-snapshot.schema.json
- [x] Tests: monitor_pr filters processed IDs, records on dispatch

## Trusted Bot Reviewers

- [x] Add TRUSTED_BOT reviewer type to ReviewerType enum
- [x] Add trusted_bot_reviewers config field
- [x] Update classify_reviewer to check trusted bot list
- [x] Update monitor_pr to treat TRUSTED_BOT as actionable (but not for approval)
- [x] Wire trusted_bot_reviewers through state
- [x] Only HUMAN approval triggers merge, not TRUSTED_BOT
- [x] Tests: 5 new classification and actionability tests

## Per-Stage Notifications

- [x] Include performer_stage in notification dedup key
- [x] Map monitoring_performer phase to card_dispatched event type
- [x] Add "ready for human review" notification on lifecycle completion
- [x] Request human reviewers via GitHub API when lifecycle completes
- [x] Update notify tests for new dedup key format

## PR-to-Issue Linkage

- [x] PR body includes "Closes #N" when issue_number is set
- [x] Existing PR body updated on re-dispatch via PATCH
- [x] Add issue_number and issue_url to Score model
- [x] Tests: PR body with/without issue number

## Architecture Documentation Trail

- [x] docs/cards/{issue}-{slug}/ folder structure (20-char slug max)
- [x] _doc_folder() helper for consistent path generation
- [x] Assessor commits assessment.md
- [x] Architect commits plan.md and tasks.md (split on ---TASKS---)
- [x] Security commits security.md
- [x] QA commits qa.md
- [x] All personas: "Do NOT commit files yourself — output only"

## Reviewer Improvements

- [x] Binary reviews — removed suggestions field
- [x] Reviewer resolves threads on approval
- [x] Reviewer persona: check prior feedback, actionable guidance on rejection
- [x] Normalize reviewer comments: strings → dicts for PerformerResponse
- [x] Tests: reviewer posts no suggestions, COMMENT event

## Closer Role

- [x] Add `closer` to lifecycle.ROLE_TO_STAGE and CANONICAL_ORDER
- [x] Add `closer` to PerformersConfig and PersonasConfig
- [x] Add `closer` persona: verify prior feedback addressed, no new nits
- [x] Map `closing_review` stage → `closer` role in dispatch_performer
- [x] Performer main: handle `closing_review` via reviewer code path
- [x] handle_blocked: ✅ Closer label
- [x] metrics: closing_review in per-role token counter
- [x] config.example.yaml: closer performer entry with explanatory comment
- [x] Tests: closer at lifecycle end, persona injection, reviewer code path

## Resolve-Threads Reliability

- [x] Detect GraphQL errors in resolve_pr_review_threads response body (was silently counting as success)
- [x] Verify isResolved=true on each mutation response
- [x] Log failed threads with id/author/outdated for diagnosis
- [x] Targets all unresolved threads regardless of comment author
- [x] Tests: 6 new tests covering author-agnostic resolution, GraphQL error detection, partial success counting

## Tech Writer

- [x] JSON output schema for tech_writer persona
- [x] _extract_json for robust parsing (same as all other roles)
- [x] Clean up docs/cards/ folder, remove stray files
- [x] Maintain docs/wiki/ project knowledge base (README, setup, testing, architecture, history)
- [x] Can break long wiki pages into subdirectories

## Dashboard

- [x] Show performer_stage and lifecycle_sequence in snapshot
- [x] Display stage with progress (e.g., "implementing (3/7)")
- [x] Handle stageIdx=-1 edge case

## Token Refresh

- [x] Auto-refresh GitHub App token during performer sessions
- [x] Coordinare sends fresh token in check_status payload every 30s
- [x] Performer updates Score.github_token on receipt

## Configuration

- [x] Example config: all 7 performer roles enabled
- [x] Example config: trusted_bot_reviewers with Copilot
- [x] Example config: GitHub App auth example
- [x] Example config: human-readable notification template
- [x] GitHub retry: 5 attempts, fibonacci backoff (1.618x)
- [x] GitHub circuit breaker: threshold 5, recovery 180s
- [x] Configurable wait_exp_base on ServiceRetryConfig
- [x] Tests: fibonacci base default, wait_exp_base wired through

## Error Reporting

- [x] Performer catch-all includes exception type and message
- [x] _doc_folder empty slug falls back to "untitled"
- [x] Security report: cast finding fields to str
- [x] Sorted processed_review_ids for deterministic serialization

## Snapshot Serialization

- [x] `_str_or_none` helper in daemon.py — None/empty coerce to JSON null, NOT literal string "None"
- [x] Fixes PR recovery regression where restart loaded pr_url/pr_node_id as string "None"
- [x] Tests: 6 new round-trip tests (None fields → null, real strings preserved, restore through disk)

## PR Recovery (monitor_pr)

- [x] `GithubService.find_pr_for_issue()` walks issue→PR linkage via closedByPullRequestsReferences
- [x] monitor_pr treats "None"/"null"/"" as missing and attempts recovery
- [x] Rehydrates card with recovered pr_url + pr_node_id
- [x] Graceful fallback to idle when recovery finds no PR
- [x] Tests: 5 monitor_pr recovery + 7 find_pr_for_issue service tests

## False-Merge Notification Suppression

- [x] notify: detect real merge via commit_summary or card.status=DONE, not phase=merging
- [x] Suppress notifications on merging-phase retries (was firing "✅ merged!" every cycle on failed retries)
- [x] check_board: clear commit_summary on new card pickup to prevent stale success detection firing for the next card
- [x] Tests: 4 notify + 2 check_board regression tests

## Permanent GitHub Error Handling

- [x] Classify TransportQueryError by GitHub error type: UNPROCESSABLE/FORBIDDEN/NOT_FOUND/UNAUTHORIZED → PermanentGitHubError
- [x] Circuit breaker `guard(ignore=...)` parameter — application-layer errors don't count as service failures
- [x] `_guarded_execute` ignores PermanentGitHubError (doesn't trip breaker on branch ruleset rejections)
- [x] merge_pr blocks with actual GitHub error message on permanent error instead of looping forever
- [x] Tests: 6 breaker ignore tests + 5 GraphQL classification + 3 merge_pr blocked transition

## Blocked-Card Dispatch Loop

- [x] handle_blocked: ALWAYS advance last_blocked_notified_at cutoff (decoupled from reminder window gate)
- [x] check_board: filter bot-authored comments by login — only humans can supply answers to blocked cards
- [x] Tests: stale-cutoff regression, bot-only comments ignored, mixed authors still trigger for humans, old human comments still gated by timestamp

## Branch Protection Awareness

- [x] Document in README that GitHub App must be in both Ruleset bypass AND classic Branch Protection "Restrict who can push" lists (they're independent)
- [x] Surface the actual GitHub error message in open_questions when permanent merge error blocks the card — human sees exactly what to fix

## Logger Bug

- [x] Replace phantom `self._logger` references in GithubService with module-level `logger` (the existing code would crash on first exception path because _logger is never initialised)

## SMTP / Mailgun

- [x] config.example.yaml: use smtp.mailgun.org example (was unresolvable smtp.example.com)
- [x] Clarify username format: `postmaster@mail.yourdomain.com`
- [x] Note EU endpoint: smtp.eu.mailgun.org
