# 040 — End-to-End Lifecycle Fixes — Implementation Plan

## Overview

All work in this plan has been completed on branch `fix/assessor-persona-and-app-auth`. This plan serves as retroactive documentation of the fixes applied during live e2e testing.

## Architecture Decisions

### AD-1: Full payload passthrough over selective field copying
`AgentService.dispatch_card()` was changed from a selective allowlist to `json.loads(json.dumps(dict(card_context), default=str))`. This ensures all fields survive the coordinare → performer boundary without requiring code changes when new fields are added.

### AD-2: COMMENT-only reviews for bot reviewer
GitHub Apps cannot APPROVE their own PRs. Rather than implementing a fallback, the reviewer always posts as COMMENT with a `**Bot Review: APPROVED/CHANGES REQUESTED**` prefix. The human reviewer handles formal approval.

### AD-3: lifecycle_completed_at cutoff for review filtering
After all lifecycle stages complete, `monitor_pr` must ignore reviews submitted before the lifecycle ran (they were already addressed). A `lifecycle_completed_at` datetime is set by `_advance_stage` and persisted in `WorkflowSnapshot`. `monitor_pr` filters reviews by comparing `submitted_at` against this cutoff.

### AD-4: Pattern recognition in implementer feedback
When addressing review comments, the implementer is instructed to identify the pattern behind the feedback and search the entire codebase for all similar occurrences. Reviewers often tag a few examples but expect all instances to be fixed.

### AD-5: Resilient graph nodes
All GitHub API calls in graph nodes are wrapped in try/except. On transient failure, nodes log a warning and return the current phase (retry next cycle) rather than crashing the LangGraph runtime.

## Files Changed (42 files, ~1300 lines)

### Coordinare Core
| File | Changes |
|------|---------|
| `src/coordinare/services/agent_service.py` | Full payload passthrough |
| `src/coordinare/services/github.py` | Inline review comments in GraphQL, review parser |
| `src/coordinare/services/persona_service.py` | Assessor, implementer, reviewer, QA, security personas |
| `src/coordinare/graph/nodes/check_board.py` | Orphan re-adoption, protected API calls |
| `src/coordinare/graph/nodes/monitor_pr.py` | lifecycle_completed_at filtering, protected API calls |
| `src/coordinare/graph/nodes/monitor_performer.py` | lifecycle_completed_at on completion |
| `src/coordinare/graph/nodes/handle_blocked.py` | Persona injection, re-queue, protected API calls |
| `src/coordinare/graph/nodes/assess_card.py` | No-questions = sufficient |
| `src/coordinare/graph/nodes/merge_pr.py` | Protected API calls |
| `src/coordinare/graph/nodes/classify_human_feedback.py` | Clear lifecycle_completed_at |
| `src/coordinare/graph/nodes/notify.py` | Human-readable notification summaries |
| `src/coordinare/graph/routing.py` | Skip assess_card when assessor in lifecycle |
| `src/coordinare/graph/state.py` | lifecycle_completed_at field |
| `src/coordinare/state_store.py` | lifecycle_completed_at persistence |
| `src/coordinare/daemon.py` | lifecycle_completed_at save/restore, readable notifications |
| `src/coordinare/__main__.py` | Readable circuit breaker notifications |
| `src/coordinare/workspace.py` | App auth token flow |
| `src/coordinare/dashboard.py` | Project board link |

### Performer
| File | Changes |
|------|---------|
| `agent/performer/src/performer/main.py` | COMMENT reviews, _extract_json for QA/security, remove thread resolution |
| `agent/performer/src/performer/github.py` | resolve_pr_review_threads, post_pull_request_review |
| `agent/performer/src/performer/workspace.py` | Branch fetch, git identity, push strategy |
| `agent/performer/src/performer/models.py` | Score model extra="ignore", all dispatch fields |
| `agent/performer/src/performer/config.py` | Timeout and check attempt tuning |
| `agent/performer/src/performer/backends/codex.py` | v2 protocol, aiohttp WS, persona/feedback in prompt |
| `agent/performer/src/performer/backends/opencode.py` | Persona/feedback in prompt, inline comments |
| `agent/performer/src/performer/backends/claude_code.py` | Persona/feedback in prompt, inline comments |

### Tests & Contracts
| File | Changes |
|------|---------|
| `specs/contracts/dispatch-payload.md` | Formal dispatch payload contract |
| `tests/contract/test_dispatch_payload.py` | 9 contract enforcement tests |

## Risk Assessment

**Low risk** — all changes are backward compatible. No database migrations, no API changes, no new dependencies. The fixes harden existing behavior discovered through live testing.
