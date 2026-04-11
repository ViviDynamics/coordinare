# 040 — End-to-End Lifecycle Fixes

## Summary

Comprehensive bug-fix and hardening pass discovered during live end-to-end testing of the coordinare against a real GitHub Project V2 board (ViviDynamics/website). A breadcrumbs feature ticket (#71) was used as the test vehicle to exercise the full lifecycle: card pickup → assessment → dispatch → implementation → CI checks → PR creation → code review → QA → human review.

## Motivation

Specs 012–024 built the performer lifecycle in isolation. When exercised against a real project with GitHub App auth, codex backend, and multi-role lifecycle (implementing → reviewing → QA), numerous integration gaps surfaced that blocked the pipeline at every stage. This spec documents and tracks all fixes applied during live testing.

## Scope

### Critical Fixes
- **Dispatch payload passthrough**: `AgentService.dispatch_card()` selectively copied fields, dropping `role`, `relay_feedback`, `pr_url`, `pr_node_id`, `backend`, `model`, `persona_instructions`. Fixed to pass through entire `card_context`.
- **Codex v2 protocol**: Updated WebSocket adapter from websockets library (incompatible with codex HTTP handshake) to aiohttp. Updated sandbox field from object to string enum (`"danger-full-access"`).
- **GitHub App auth flow**: Workspace manager wired to use App installation tokens for git operations. Performer subprocess receives token via dispatch payload, not leaked env vars.

### Performer Lifecycle Fixes
- **Orphaned card re-adoption**: After stateless restart, IN_PROGRESS cards are re-adopted by `check_board`.
- **Branch preservation**: Fetch existing remote branch on re-dispatch instead of creating new from main (which caused PRs to be closed due to zero diff).
- **PR thread resolution removed**: Bot was resolving human review threads without fixing code. Removed — humans verify and resolve their own threads.
- **Review loop prevention**: Added `lifecycle_completed_at` timestamp to filter old PR reviews after all lifecycle stages complete, preventing infinite re-dispatch loops.
- **Reviewer self-approve**: GitHub Apps cannot APPROVE their own PRs. Reviewer now always posts as COMMENT with verdict prefix.
- **QA/Security JSON parsing**: Codex outputs prose, not JSON. Applied `_extract_json()` helper to all role output parsers (reviewer, QA, security).

### Persona & Feedback Improvements
- **Assessor persona**: Rewritten as pragmatic PM — focus on user intent, not implementation details. Wired as proper performer role.
- **Implementer persona**: Pattern recognition — when addressing review feedback, search entire codebase for all similar occurrences, not just tagged lines.
- **Reviewer persona**: Meticulous review checklist — flag linter disables, convention violations, readability issues, correctness, test quality. No rubber-stamping.
- **Inline review comments**: GraphQL query now fetches PR review inline comments (path, line, body). All 3 backends (codex, opencode, claude_code) include file:line references in relay feedback.

### Resilience & Observability
- **DNS failure resilience**: All graph nodes now wrap GitHub API calls in try/except. Previously, `monitor_pr` and `merge_pr` had unprotected calls that crashed the LangGraph runtime on transient DNS failures.
- **Human-readable notifications**: Notification summaries use emoji, issue numbers, truncated titles, and contextual messages instead of raw key-value dumps.
- **Dashboard**: Added project board link in header.
- **Contract tests**: Formal dispatch payload contract spec with 9 enforcement tests.

### Configuration & Defaults
- **Model update**: `claude-3-5-sonnet-latest` → `claude-sonnet-4-20250514`.
- **Timeout tuning**: `AGENT_TIMEOUT` 30min → 120min, `CHECK_MAX_ATTEMPTS` 3 → 25.
- **Git identity**: Performer sets `vivi-coordinare[bot]` identity before backend starts.
- **Performer wrapper script**: `bin/performer` for subprocess transport.

## Out of Scope
- Email (SMTP) notification delivery (Mailtrap "Unexpected EOF" — separate infra issue)
- Card stuck threshold tuning for long-running codex sessions
- Dashboard backend transparency (surfacing codex web UI URLs)
