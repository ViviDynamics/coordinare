# Implementation Plan: Board Orchestrator Daemon

**Branch**: `001-board-orchestrator` | **Date**: 2026-02-16 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-board-orchestrator/spec.md`

## Summary

Build a long-running Python daemon that orchestrates a GitHub Projects board workflow. The coordinare monitors a single board, dispatches cards one-at-a-time to AI coding agents via SSH, manages the PR review cycle (filtering human vs bot reviews), handles blocked cards with clarifying questions, sends email and Slack notifications on all transitions, and exposes health-check and metrics endpoints. Built with LangGraph for stateful workflow orchestration with crash-recovery checkpointing, the `anthropic` SDK for AI-powered card analysis, and `gql` for GitHub Projects GraphQL API integration.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: LangGraph (>=0.2), anthropic (>=0.40), gql[aiohttp], asyncssh, FastAPI, structlog, slack-sdk, aiosmtplib, prometheus-client, pydantic-settings
**Storage**: PostgreSQL (LangGraph checkpoint persistence for crash recovery); SQLite for development/testing
**Testing**: pytest + pytest-asyncio, ruff (lint/format), mypy (type checking)
**Target Platform**: Linux server (Docker container, deployable via Docker Compose or Kubernetes)
**Project Type**: Single project
**Performance Goals**: Card pickup within 60s of columns clearing (SC-001); notifications within 2min (SC-002); PR merge within 5min of approval (SC-004); restart recovery within 60s (SC-006)
**Constraints**: Single-card-at-a-time processing; GitHub GraphQL rate limit of 5,000 points/hour; polling interval 30-60s
**Scale/Scope**: One board, one project, one agent at a time; ~50 cards/board maximum; 5 board columns

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Principle I: Code Quality First
- [x] **Readability over cleverness**: Explicit async node functions, typed state schema, clear separation of concerns
- [x] **Single Responsibility**: Each LangGraph node handles one action (check board, dispatch card, monitor PR, etc.); services layer isolates external integrations
- [x] **Consistent style**: ruff for formatting/linting, enforced in CI
- [x] **No dead code**: Clean module boundaries; no speculative abstractions
- [x] **Minimal dependencies**: Each dependency justified (see research.md R6); no langchain, no crewai, no PyGithub
- [x] **Type safety**: mypy strict mode; all public interfaces typed; Pydantic models for config and data

### Principle II: Testing Discipline (NON-NEGOTIABLE)
- [x] **Unit tests**: Every node function, service method, and utility tested with mocked dependencies
- [x] **Integration tests**: Graph execution with `MemorySaver`; service integration with mock GitHub/SSH/SMTP/Slack
- [x] **Contract tests**: GitHub GraphQL query/mutation schemas validated; agent CLI interface contracts
- [x] **Coverage threshold**: Configured minimum; CI blocks on regression
- [x] **Deterministic tests**: All external services mocked; no network calls in tests

### Principle III: User Experience Consistency
- [x] **Consistent patterns**: Notification format standardized across email and Slack
- [x] **Error communication**: Structured error messages in notifications; card comments with actionable questions
- [x] **Loading and state feedback**: Health-check endpoint reports current status; metrics dashboard
- [ ] **Accessibility / Responsive**: N/A (no UI; daemon with API endpoints only)

### Principle IV: Performance by Design
- [x] **Budgets**: SC-001 through SC-006 define measurable performance targets
- [x] **Measurement**: Prometheus metrics track card cycle time, dispatch latency, notification timing
- [x] **Efficient by default**: Lightweight poll queries (~3-5 GraphQL points); separate detail fetches only when needed
- [x] **Caching strategy**: Cache GitHub field/option IDs at startup; refresh on stale-ID errors

### Principle V: Clarity Before Action
- [x] **Mark unknowns explicitly**: All NEEDS CLARIFICATION resolved in research.md
- [x] **Block on ambiguity**: No unresolved items remain
- [x] **Document resolutions**: All decisions recorded in research.md and spec clarifications

### Quality Gates
- [x] Lint & Format (ruff)
- [x] Type Check (mypy)
- [x] Unit Tests (pytest)
- [x] Integration Tests (pytest)
- [x] Coverage Check (configured minimum)
- [x] Performance Check (metrics-based benchmarks for SC-001 through SC-006)
- [ ] Accessibility Check: N/A (no UI)
- [x] Code Review (PR-based, per constitution)

**Gate Status**: PASS — No violations. Principle III accessibility items are N/A for a headless daemon.

## Project Structure

### Documentation (this feature)

```text
specs/001-board-orchestrator/
├── plan.md              # This file
├── research.md          # Phase 0: Technology research and decisions
├── data-model.md        # Phase 1: Entity definitions and state transitions
├── quickstart.md        # Phase 1: Developer setup and running guide
├── contracts/           # Phase 1: API and interface contracts
│   ├── github-graphql.md
│   ├── agent-ssh-interface.md
│   └── health-api.md
└── tasks.md             # Phase 2: Task breakdown (/speckit.tasks)
```

### Source Code (repository root)

```text
src/
├── coordinare/
│   ├── __init__.py
│   ├── __main__.py          # Entry point: daemon startup
│   ├── daemon.py            # Async daemon loop, signal handling, lifecycle
│   ├── config.py            # Pydantic-settings: layered config (file + env vars)
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py         # CoordinareState TypedDict
│   │   ├── builder.py       # StateGraph construction and compilation
│   │   ├── routing.py       # Conditional edge functions
│   │   └── nodes/
│   │       ├── __init__.py
│   │       ├── check_board.py     # Poll GitHub Projects board status
│   │       ├── assess_card.py     # Claude-powered card sufficiency analysis
│   │       ├── dispatch_card.py   # Move card to In Progress, SSH dispatch to agent
│   │       ├── monitor_agent.py   # Check agent status via SSH
│   │       ├── monitor_pr.py      # Check PR reviews, classify human vs bot
│   │       ├── relay_feedback.py  # Send review feedback to agent
│   │       ├── merge_pr.py        # Squash-merge PR, move card to Done
│   │       ├── handle_blocked.py  # Move to Blocked, post questions, notify
│   │       └── notify.py          # Send email + Slack notifications
│   ├── services/
│   │   ├── __init__.py
│   │   ├── github.py         # GitHub Projects GraphQL client (gql)
│   │   ├── agent_ssh.py      # SSH-based agent dispatch and status (asyncssh)
│   │   ├── email.py          # SMTP email notifications (aiosmtplib)
│   │   ├── slack.py          # Slack notifications (slack-sdk)
│   │   └── claude.py         # Anthropic Claude reasoning (anthropic SDK)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── card.py           # Card entity, status enum, transition history
│   │   ├── project.py        # ProjectConfiguration model
│   │   ├── notification.py   # Notification message model
│   │   └── review.py         # PR review model (human/bot classification)
│   ├── health.py             # FastAPI health-check endpoint (FR-021)
│   └── metrics.py            # Prometheus metrics export (FR-022)
│
tests/
├── conftest.py              # Shared fixtures (mock GitHub, mock SSH, mock SMTP)
├── unit/
│   ├── test_config.py
│   ├── test_state.py
│   ├── graph/
│   │   └── nodes/
│   │       ├── test_check_board.py
│   │       ├── test_assess_card.py
│   │       ├── test_dispatch_card.py
│   │       ├── test_monitor_pr.py
│   │       ├── test_relay_feedback.py
│   │       ├── test_merge_pr.py
│   │       ├── test_handle_blocked.py
│   │       └── test_notify.py
│   ├── services/
│   │   ├── test_github.py
│   │   ├── test_agent_ssh.py
│   │   ├── test_email.py
│   │   ├── test_slack.py
│   │   └── test_claude.py
│   └── models/
│       ├── test_card.py
│       └── test_review.py
├── integration/
│   ├── test_graph_execution.py    # Full graph with MemorySaver
│   ├── test_daemon_lifecycle.py   # Startup, shutdown, restart recovery
│   └── test_notification_flow.py  # End-to-end notification pipeline
└── contract/
    ├── test_github_queries.py     # GraphQL query/mutation schema validation
    ├── test_agent_interface.py    # SSH command contract
    └── test_health_api.py         # Health endpoint response schema
```

**Structure Decision**: Single project layout. The coordinare is a standalone Python package (`src/coordinare/`) with a flat services layer. No frontend, no mobile, no multi-package workspace. The `src/` layout enables proper package installation and import resolution. Tests mirror the source structure.

## Complexity Tracking

> No constitution violations requiring justification. All principles satisfied as documented in the Constitution Check above.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| *(none)* | — | — |
