# 040 — End-to-End Lifecycle Fixes — Tasks

All tasks completed on branch `fix/assessor-persona-and-app-auth`.

## Critical Fixes

- [x] Fix AgentService.dispatch_card() to pass through ALL card_context fields (not selective allowlist)
- [x] Update codex adapter to v2 protocol (sandbox string enum, developerInstructions, aiohttp WebSocket)
- [x] Wire GitHub App auth token flow through workspace manager to performer subprocess
- [x] Update default model from claude-3-5-sonnet-latest to claude-sonnet-4-20250514

## Performer Lifecycle

- [x] Re-adopt orphaned IN_PROGRESS cards after stateless restart in check_board
- [x] Fetch existing remote branch on re-dispatch instead of creating new from main
- [x] Set git identity to vivi-coordinare[bot] in workspace before backend starts
- [x] Remove automatic PR thread resolution — humans verify and resolve their own threads
- [x] Add lifecycle_completed_at timestamp to prevent review re-dispatch loop after lifecycle completion
- [x] Persist lifecycle_completed_at in WorkflowSnapshot and restore on daemon restart
- [x] Reviewer always posts as COMMENT (not APPROVE/REQUEST_CHANGES) — human handles formal approval
- [x] Apply _extract_json() to QA and security output parsing (same robust parser as reviewer)
- [x] Capture codex turn output from items, accumulated deltas, and summary

## Persona & Feedback

- [x] Rewrite assessor persona as pragmatic PM — focus on user intent, not implementation details
- [x] Wire assessor as proper performer role; skip legacy assess_card when assessor in lifecycle
- [x] Inject assessor persona into handle_blocked assessment call
- [x] Harden reviewer persona — flag linter disables, convention violations, readability, test quality
- [x] Update implementer persona — pattern recognition on review feedback, fix all similar occurrences
- [x] Add JSON output format instructions to reviewer, QA, and security personas
- [x] Fetch inline PR review comments (path, line, body) via GraphQL
- [x] Include inline comments with file:line references in relay feedback for all 3 backends

## Resilience & Observability

- [x] Wrap all GitHub API calls in graph nodes with try/except (monitor_pr, merge_pr, handle_blocked, check_board)
- [x] Never post generic "clarify acceptance criteria" — re-queue to TODO instead
- [x] Human-readable notification summaries with emoji, issue numbers, truncated titles
- [x] Add project board link to dashboard header
- [x] Increase AGENT_TIMEOUT from 30min to 120min
- [x] Increase CHECK_MAX_ATTEMPTS from 3 to 25
- [x] Add performer wrapper script (bin/performer) for subprocess transport

## Testing & Contracts

- [x] Create formal dispatch payload contract spec (specs/contracts/dispatch-payload.md)
- [x] Write 9 contract enforcement tests for coordinare → performer boundary
- [x] Show correct performer role in blocked comments (not always Assessor)
- [x] Treat no-questions assessment as sufficient (don't block on empty result)
