# Feature Specification: Dry-Run Mode

**Feature Branch**: `038-dry-run-mode`
**Created**: 2026-03-24
**Status**: Draft

## Overview

Operators currently have no way to preview what coordinare would do with a given card without actually dispatching performers and modifying the GitHub board. This feature adds a dry-run mode that runs the coordinare graph in a sandboxed preview, replacing all side-effecting services (GitHub API, performer dispatch) with logging stubs that record intended actions. The output is a human-readable plan of what would happen, printed to stdout (CLI) or rendered in the dashboard (web).

## Clarifications

### Session 2026-03-24

- Q: Should dry-run mode run the full graph or just the dispatch decision? → A: It runs the full graph once — from card assessment through dispatch decision and lifecycle stage planning. It does not actually wait for performer completion (since no performer is running).
- Q: Does dry-run modify any state? → A: No. Dry-run uses a throwaway copy of `CoordinareState`. No state file is written, no board columns are moved, no performers are dispatched.
- Q: Should the dashboard button be real-time or a one-shot result? → A: One-shot. The button triggers a single dry-run pass and displays the result. No streaming.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — CLI Dry-Run Command (Priority: P1)

An operator runs `coordinare dry-run --card <card_id>` from the terminal. The coordinare loads config, constructs the graph with mock services, runs a single pass for the specified card, and prints the planned actions to stdout. No GitHub API calls are made, no performers are launched, and no state is persisted.

**Why this priority**: CLI dry-run is the foundational capability. It is the simplest interface and enables scripting and CI integration.

**Independent Test**: Run the dry-run command with a mock config and a fake card ID. Verify the output contains the expected planned actions and that no side-effecting service methods were called.

**Acceptance Scenarios**:

1. **Given** a valid card ID and config, **When** the operator runs `coordinare dry-run --card <id>`, **Then** stdout shows the planned lifecycle stages, dispatch parameters, and board column transitions.
2. **Given** a card that would be assessed as insufficient, **When** dry-run executes, **Then** the output shows the card would be moved to Blocked with the assessment reason.
3. **Given** a card with all lifecycle roles configured, **When** dry-run executes, **Then** the output lists each role in sequence with its backend and model.
4. **Given** an invalid card ID, **When** dry-run executes, **Then** it exits with a clear error message without modifying any state.

---

### User Story 2 — Dashboard Dry-Run Button (Priority: P2)

The web dashboard shows a "Dry Run" button next to each card in the board view. Clicking it sends an API request that triggers a dry-run pass for that card and displays the result in a modal overlay.

**Why this priority**: The dashboard button is a convenience feature that builds on the CLI dry-run capability. It is not required for basic operation.

**Independent Test**: Send a POST to `/api/dry-run/<card_id>`, verify the response contains the planned actions as JSON, and verify no side effects occurred.

**Acceptance Scenarios**:

1. **Given** the dashboard is running, **When** the operator clicks "Dry Run" on a card, **Then** a modal displays the planned lifecycle and actions.
2. **Given** the dry-run API endpoint receives a request, **When** processing completes, **Then** it returns JSON with `planned_actions`, `lifecycle_stages`, and `assessment_result`.
3. **Given** an error during dry-run (e.g. config issue), **When** the API is called, **Then** it returns a 4xx/5xx with a clear error message.

---

### Edge Cases

- What if the card is already in progress with a running performer? Dry-run still shows what would happen if the card were re-dispatched from scratch.
- What if the card has no GitHub issue associated? Dry-run reports this as an assessment failure.
- What if config is invalid? Dry-run fails fast with a config validation error before attempting any graph execution.
- What if the assessment backend is `anthropic_api`? In dry-run mode, the assessment backend is also stubbed — it returns a synthetic "sufficient" result to show the full lifecycle plan.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST support a `dry-run` CLI subcommand that accepts a `--card <card_id>` argument.
- **FR-002**: In dry-run mode, the coordinare MUST replace `GitHubService` with a `DryRunGitHubService` that logs all intended API calls without executing them.
- **FR-003**: In dry-run mode, the coordinare MUST replace all performer `AgentService` instances with a `DryRunAgentService` that logs dispatch parameters and returns synthetic success responses.
- **FR-004**: In dry-run mode, the coordinare MUST NOT write to the state file, move board columns, or create any GitHub resources.
- **FR-005**: The dry-run output MUST include: the card's assessment result, the planned lifecycle sequence, the backend and model for each role, and the expected board column transitions.
- **FR-006**: The dashboard MUST expose a `POST /api/dry-run/<card_id>` endpoint that returns the dry-run result as JSON.
- **FR-007**: The dashboard MUST render a "Dry Run" button per card that calls the endpoint and displays the result.
- **FR-008**: Dry-run MUST use a throwaway copy of `CoordinareState` so no in-memory state is modified.

### Key Entities

- **DryRunGitHubService**: Drop-in replacement for `GitHubService` that records calls instead of executing them.
- **DryRunAgentService**: Drop-in replacement for `AgentService` / `ResilientAgentService` that records dispatch parameters and returns synthetic success.
- **DryRunResult**: Pydantic model containing `planned_actions: list[str]`, `lifecycle_stages: list[str]`, `assessment_result: str`, `board_transitions: list[dict]`.
- **`coordinare dry-run`**: New CLI subcommand.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `coordinare dry-run --card <id>` produces output within 2 seconds (no network calls).
- **SC-002**: Zero GitHub API calls are made during a dry-run execution.
- **SC-003**: Zero performer processes are spawned during a dry-run execution.
- **SC-004**: The dry-run output accurately reflects the lifecycle that would execute for the given card and config.
- **SC-005**: The dashboard dry-run button returns a result within 2 seconds.

## Assumptions

- The graph is deterministic enough that a dry-run accurately predicts the real execution path for the dispatch and lifecycle planning phases.
- Dry-run does not predict performer output (e.g., what code the implementer would write). It only shows what the coordinare would do: which roles would be dispatched, in what order, with what parameters.
- The assessment backend stub always returns "sufficient" to show the full lifecycle. A future enhancement could allow the operator to choose the assessment outcome.
