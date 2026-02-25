# Implementation Plan: Customer Advocate Agent

**Branch**: `007-customer-advocate-agent` | **Date**: 2026-02-24 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/007-customer-advocate-agent/spec.md`

## Summary

Extend the coordinare board orchestrator with a `customer_advocate` LangGraph node that scans open GitHub issues on every poll cycle, classifies them using Claude, and takes appropriate action: auto-replies to question/confusion issues grounded in configured documentation, escalates sensitive or low-confidence issues to human reviewers via Slack/email, acknowledges feature requests, triages bug reports silently, and redirects off-topic posts. Uses a label-first approach (`advocate-handled` / `needs-human`) to prevent duplicate processing across overlapping poll cycles. Implements a pluggable multi-LLM confidence scoring interface with Claude as the V1 provider.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: LangGraph (>=0.2), anthropic (>=0.40), gql[aiohttp], structlog, slack-sdk, aiosmtplib, pydantic-settings — all existing; no new dependencies for V1
**Storage**: In-memory set (response history keyed by issue ID, per coordinare session); no persistent storage in this spec (cross-session persistence deferred to spec 003)
**Testing**: pytest + pytest-asyncio, ruff, mypy (same stack as 001)
**Target Platform**: Linux server (Docker container, same as 001)
**Project Type**: Single project (extending existing `src/coordinare/` package)
**Performance Goals**: SC-006 — ≤10s additional latency per poll cycle for up to 20 new issues (parallel classification with asyncio.gather); SC-004 — escalation notifications within one poll cycle (≤60s)
**Constraints**: GitHub GraphQL rate limits (5,000 points/hour); label-first deduplication; in-memory history resets on restart; single-repo scope; Claude API latency ~0.5s/call (mitigated by parallel processing)
**Scale/Scope**: Up to 20 unprocessed issues/cycle; single GitHub repository; ≤100 open issues total

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Principle I: Code Quality First
- [x] **Readability over cleverness**: Explicit async advocate node, typed models, clear classification → action pipeline
- [x] **Single Responsibility**: `advocate.py` node handles orchestration; `scoring.py` handles LLM scoring; `models/advocate.py` holds data types; `GitHubService` extended with advocate-specific GraphQL queries
- [x] **Consistent style**: ruff for formatting/linting, enforced in CI
- [x] **No dead code**: ScoringProvider protocol designed for V1 Claude only; additional providers added only when configured via config
- [x] **Minimal dependencies**: Zero new dependencies for V1; existing anthropic SDK + gql + aiohttp cover all needs
- [x] **Type safety**: mypy strict mode; all public interfaces typed; Pydantic models for AdvocateConfig and all entities

### Principle II: Testing Discipline (NON-NEGOTIABLE)
- [x] **Unit tests**: Every advocate node function, scoring service, and model tested with mocked GitHub/Claude
- [x] **Integration tests**: Full advocate scan cycle with MemorySaver; mock issue responses; mock GitHub labels
- [x] **Contract tests**: GraphQL queries for issue scanning, label add, file fetch validated against schemas
- [x] **Coverage threshold**: No regression; new code meets configured minimum
- [x] **Deterministic tests**: GitHub and Claude services mocked; no network calls in tests

### Principle III: User Experience Consistency
- [x] **Consistent patterns**: Comment format follows the same structured style as board workflow notifications
- [x] **Error communication**: Structured error log on API failure; escalation on Claude API failure (not silent failure)
- [x] **Loading and state feedback**: Structured log emitted for each issue processed with action taken
- [ ] **Accessibility / Responsive**: N/A (headless daemon; GitHub comment output)

### Principle IV: Performance by Design
- [x] **Budgets**: SC-006 (≤10s/20 issues) and SC-004 (≤60s notification delivery) defined in spec
- [x] **Measurement**: Prometheus counter for issues processed, classified, escalated; latency histogram for advocate scan duration
- [x] **Efficient by default**: Parallel asyncio.gather for issue classification (bounded concurrency semaphore); label applied before any comment post
- [x] **Caching strategy**: Label IDs cached at startup (same pattern as field IDs in 001); doc file contents fetched fresh each poll cycle so document changes are picked up without restart

### Principle V: Clarity Before Action
- [x] **Mark unknowns explicitly**: All clarifications resolved in spec session 2026-02-23; research.md resolves remaining implementation decisions
- [x] **Block on ambiguity**: No `NEEDS CLARIFICATION` tags remain
- [x] **Document resolutions**: research.md captures all technology and design decisions

### Quality Gates
- [x] Lint & Format (ruff)
- [x] Type Check (mypy)
- [x] Unit Tests (pytest)
- [x] Integration Tests (pytest)
- [x] Coverage Check (configured minimum)
- [x] Performance Check (advocate scan latency metric via Prometheus)
- [ ] Accessibility Check: N/A (no UI)
- [x] Code Review (PR-based, per constitution)

**Gate Status**: PASS — No violations. Principle III accessibility and IV accessibility items are N/A for a headless daemon.

## Project Structure

### Documentation (this feature)

```text
specs/007-customer-advocate-agent/
├── plan.md              # This file
├── research.md          # Phase 0: Implementation decisions
├── data-model.md        # Phase 1: Entity definitions and config schema
├── quickstart.md        # Phase 1: Developer setup and testing guide
├── contracts/           # Phase 1: GraphQL and protocol contracts
│   ├── github-graphql-advocate.md   # Issue scan, label, file fetch queries/mutations
│   └── scoring-provider.md         # ScoringProvider protocol interface
└── tasks.md             # Phase 2: Task breakdown (/speckit.tasks)
```

### Source Code (repository root)

```text
src/
└── coordinare/
    ├── config.py              # ADD: AdvocateConfig nested Pydantic model; advocate field
    ├── graph/
    │   ├── builder.py         # ADD: advocate_scan node; START → advocate_scan → check_board
    │   ├── state.py           # ADD: advocate_service protocol field; advocate_history set
    │   └── nodes/
    │       └── advocate.py    # NEW: customer advocate LangGraph node
    ├── models/
    │   └── advocate.py        # NEW: IssueClassification, AdvocateResponse, EscalationRecord,
    │                          #      DocumentationSource, ScoringProvider, ConsensusScore,
    │                          #      IssueType enum, AdvocateAction enum, EscalationReason enum
    └── services/
        ├── github.py          # ADD: list_open_issues(), add_labels(), get_file_content(),
        │                      #      ensure_labels_exist(), get_label_ids(), get_repository_id()
        ├── advocate.py        # NEW: AdvocateService (scan_and_respond orchestration)
        └── scoring.py         # NEW: ScoringProviderProtocol, ClaudeScorer (V1)

tests/
├── unit/
│   ├── graph/nodes/
│   │   └── test_advocate.py             # Advocate node unit tests (all branches)
│   ├── models/
│   │   └── test_advocate_models.py      # Model validation and enum tests
│   └── services/
│       └── test_scoring.py              # ClaudeScorer unit tests
├── integration/
│   └── test_advocate_scan.py            # Full advocate cycle with mocked services
└── contract/
    └── test_github_advocate_queries.py  # GraphQL query/mutation schema validation
```

**Structure Decision**: Single project extension. No new packages or services. The advocate node integrates into the existing graph as a new node inserted before `check_board`. All new code follows the established patterns in `src/coordinare/`.

## Complexity Tracking

> No constitution violations requiring justification.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| *(none)*  | —          | —                                    |
