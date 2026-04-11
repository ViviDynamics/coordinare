# Contract: Dispatch Payload (Coordinare → Performer)

**Last updated**: 2026-04-06
**Boundary**: `AgentService.dispatch_card()` → wire (subprocess stdin) → `Score(**payload)` in performer

## Overview

The dispatch payload is the primary data contract between the coordinare and performer.
It flows through `AgentService.dispatch_card()`, is serialized as JSON over the subprocess
transport, and deserialized into the performer's `Score` pydantic model.

**Critical rule**: `AgentService.dispatch_card()` MUST pass through ALL `card_context` fields.
It MUST NOT selectively filter fields. The `Score` model on the performer side is the
authoritative schema — any field not on `Score` is silently dropped by pydantic (`extra="ignore"`).

## Field Registry

### Card Identity (set by check_board)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `id` | str | yes | check_board | performer logging |
| `title` | str | yes | check_board | backend prompt, PR title |
| `description` | str | yes | check_board | backend prompt |
| `acceptance_criteria` | list[str] | no | check_board | backend prompt |
| `status` | str | no | check_board | informational |
| `previous_status` | str | no | check_board | informational |
| `issue_id` | str | no | check_board | issue details lookup |
| `issue_number` | int | no | check_board | informational |
| `issue_url` | str | no | check_board | informational |

### Workspace (set by WorkspaceManager, overlaid by AgentService)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `repo_url` | str | yes | WorkspaceManager | git clone/push, Score validation |
| `branch` | str | yes | WorkspaceManager | git checkout, push, PR head |
| `github_token` | str | yes* | WorkspaceManager | git auth, GitHub API calls |
| `workspace_path` | str | no | WorkspaceManager | informational (not used by performer) |

*Empty string allowed when K8s transport injects auth via secrets.

### Performer Lifecycle (set by dispatch_performer)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `role` | str | yes | dispatch_performer | role-specific status handling (architect, reviewer, security, QA, assessor, etc.) |
| `persona_instructions` | str | no | dispatch_performer | backend prompt — role-specific behavior |
| `relay_feedback` | list[dict] | no | dispatch_performer | backend prompt — human review comments to address |
| `pr_url` | str | no | dispatch_performer (from card) | reviewer/security post reviews to PR |
| `pr_node_id` | str | no | dispatch_performer (from card) | terminal status response for coordinare |
| `architecture_plan_path` | str | no | dispatch_performer (from card) | backend prompt — reference architect's plan |
| `clarifications` | list[dict] | no | assess_card (embedded in card) | backend prompt — Q&A history |

### Backend Selection (set by dispatch_performer, feature 037)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `backend` | str | no | dispatch_performer | select AI backend (opencode, claude_code, codex) |
| `model` | str | no | dispatch_performer | select model within backend |

### GitHub Enterprise (set by dispatch_performer, feature 036)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `github_api_url` | str | no | dispatch_performer | override GitHub REST API base URL |

## Enforcement Points

### 1. AgentService.dispatch_card() — `src/coordinare/services/agent_service.py`

MUST pass through `dict(card_context)` without field filtering.
Workspace fields from `WorkspaceInfo` are overlaid on top.

### 2. Score model — `agent/performer/src/performer/models.py`

MUST have explicit fields for every payload key the performer needs to read.
Uses `model_config = {"extra": "ignore"}` — unknown fields are silently dropped.
When adding a new field to the dispatch payload, it MUST also be added to Score.

### 3. Backend prompt builders

Each backend (`opencode.py`, `claude_code.py`, `codex.py`) has a `_build_task_prompt(score)`
function that constructs the prompt sent to the AI. It MUST include:
- `score.persona_instructions` (role-specific behavior)
- `score.relay_feedback` (human review comments)
- `score.acceptance_criteria`
- `score.clarifications` (Q&A history)
- `score.architecture_plan_path` (when present)

### 4. Contract tests — `tests/contract/test_dispatch_payload.py`

Integration tests that verify the full pipeline:
`card_context` → `AgentService.dispatch_card()` → wire → `Score(**payload)`

These tests MUST assert that every field in this registry survives the journey.

## Change Protocol

When adding a new field to the dispatch payload:

1. Add the field to this contract document
2. Add the field to `card_context` in `dispatch_performer.py`
3. Add the field to `Score` in `agent/performer/src/performer/models.py`
4. Add the field to the relevant backend `_build_task_prompt()` if the AI needs it
5. Add a contract test assertion in `tests/contract/test_dispatch_payload.py`
6. Run contract tests to verify end-to-end delivery
