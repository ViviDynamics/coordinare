# Implementation Plan: Dry-Run Mode

**Branch**: `038-dry-run-mode` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add a `coordinare dry-run --card <card_id>` CLI subcommand and a dashboard API endpoint that run the coordinare graph with mock services (no GitHub API calls, no performer spawns, no state persistence). Three new files for mock services and CLI handler; one dashboard route added. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: FastAPI, pydantic, structlog, argparse (all existing) — **no new dependencies**
**Storage**: N/A — dry-run is stateless by design
**Constraints**: Must not modify external state; must reuse existing graph infrastructure
**Scale/Scope**: 3 new files, 2 modified files, ~10 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Mock services implement existing protocols; no special-case graph branches |
| II. Testing Discipline | PASS | Mock services inherently testable; output validated in unit tests |
| III. User Experience | PASS | Clear CLI output; dashboard button for operators |
| IV. Performance by Design | PASS | No network I/O; in-memory only |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/dry_run.py                         # NEW — DryRunGitHubService, DryRunAgentService, DryRunResult, execute_dry_run()
src/coordinare/__main__.py                        # MODIFIED — add dry-run subcommand to argparse
src/coordinare/dashboard/app.py                   # MODIFIED — add POST /api/dry-run/<card_id> endpoint
tests/unit/test_dry_run.py                       # NEW — mock services and execute_dry_run tests
tests/unit/test_dry_run_cli.py                   # NEW — CLI argument parsing and integration
tests/unit/dashboard/test_dry_run_endpoint.py    # NEW — dashboard API endpoint tests
```

## Detailed Implementation Plan

### Step 1 — Dry-Run Mock Services (`src/coordinare/dry_run.py`)

Create `DryRunGitHubService` implementing `GitHubServiceProtocol` — every method appends to `recorded_actions` and returns synthetic data. Create `DryRunAgentService` implementing the agent service protocol — `dispatch_card` records params and returns `{"accepted": True}`, `check_status` returns `{"status": "pr_opened"}`. Create `DryRunResult(BaseModel)` with `planned_actions`, `lifecycle_stages`, `assessment_result`, `board_transitions`. Add `execute_dry_run(card_id, config)` that builds the graph with mock services, runs one pass, and collects recorded actions into a `DryRunResult`.

### Step 2 — CLI Subcommand (`src/coordinare/__main__.py`)

Add `dry-run` subcommand to argparse: `--card` (required) and `--config` (optional). Handler loads config, calls `execute_dry_run()`, and prints the result to stdout in human-readable format.

### Step 3 — Dashboard API Endpoint (`src/coordinare/dashboard/app.py`)

Add `POST /api/dry-run/{card_id}` that calls `execute_dry_run()` and returns `DryRunResult` as JSON. Add a "Dry Run" button to the card row in the dashboard HTML that calls the endpoint and shows results in a modal.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| 1 new module (dry_run.py) | ~120 LOC | All mock services and orchestration; isolated from production paths |
| CLI subcommand | ~30 LOC | Reuses existing argparse structure |
| Dashboard endpoint + button | ~25 LOC | Thin wrapper around execute_dry_run |
