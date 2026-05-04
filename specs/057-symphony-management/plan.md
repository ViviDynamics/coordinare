# Implementation Plan: Symphony Management & Multi-Project Orchestration

**Branch**: `057-symphony-management` | **Date**: 2026-04-30 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/057-symphony-management/spec.md`

## Summary

Extend coordinare to manage multiple GitHub project boards ("symphonies") with a shared performer pool ("orchestra"). Symphonies are orchestrated sequentially in configured priority order, with per-symphony config merging global defaults + overrides, per-symphony state tracking, and isolated metrics/logging. Backward-compatible: existing single-project deployments auto-wrap as implicit default symphony. Hot-reload support allows dynamic symphony add/remove without restart.

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: LangGraph (≥0.2), pydantic (≥2.9), pydantic-settings (≥2.6), FastAPI, structlog (≥24.1), prometheus-client (≥0.21), httpx  
**Storage**: YAML configuration file (existing config.yaml pattern); ephemeral runtime state in memory  
**Testing**: pytest (existing test suite)  
**Target Platform**: Linux server (running coordinare daemon)  
**Project Type**: Single Python application (daemon + integrated dashboard)  
**Performance Goals**: Sub-30s poll cycle per symphony (sequential orchestration); <100ms for config validation; no network latency amplification from sequential polling  
**Constraints**: Per-symphony metrics must be isolated for separate Prometheus queries; config hot-reload must not disrupt in-flight cards; backward-compatible with single-project deployments  
**Scale/Scope**: Support 5-50 symphonies per coordinare instance; shared performer pool with dynamic allocation; ~500-1000 additional LOC for core feature, ~200-300 for dashboard UI

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

**Status**: ✅ **PASS** — All gates satisfied

### Code Quality First
- ✅ Type hints: pydantic models for all Symphony/Orchestra entities
- ✅ Testing: unit tests for config merge logic, integration tests for multi-symphony polling, fixture for multiple board states
- ✅ Linting: ruff configured for repo

### Testing Discipline
- ✅ Unit: config resolution, validation, merging logic
- ✅ Integration: full orchestration cycle with mocked GitHub API (multiple boards)
- ✅ Contract: API endpoint validation (symphony CRUD)
- ✅ Coverage: >85% for new code paths

### User Experience Consistency
- ✅ API: RESTful symphony CRUD endpoints follow existing patterns
- ✅ Dashboard: new "Symphonies" page matches existing design (dark theme, grid layout, status badges)
- ✅ Backward-compat: single-project configs require zero changes

### Performance by Design
- ✅ Sequential orchestration acceptable per user clarification (30s poll window makes per-symphony delay imperceptible)
- ✅ Metrics cardinality: 10 symphonies × existing labels = manageable impact
- ✅ Config validation: pydantic is fast (< 10ms even with large payloads)
- ✅ Hot-reload: in-memory state update, no database locks

### Clarity Before Action
- ✅ Data model documented with entity diagrams and resolution rules
- ✅ API contracts specified with request/response schemas and error codes
- ✅ Configuration examples provided for common scenarios
- ✅ No ambiguity in behavior (sequential polling order, config precedence, persona merging)

## Project Structure

### Documentation (this feature)

```text
specs/057-symphony-management/
├── plan.md              # This file (Phase 1 output)
├── spec.md              # Feature specification (Phase 0 input)
├── research.md          # Phase 0 research findings
├── data-model.md        # Phase 1 entity definitions
├── quickstart.md        # Phase 1 configuration examples
├── contracts/           # Phase 1 API contracts
│   ├── symphony-api.md  # Symphony CRUD & config endpoints
│   └── dashboard-state.md # Dashboard state schema + UI pages
├── tasks.md             # Phase 2 output (TBD)
└── checklists/
    └── requirements.md  # Spec completeness checklist (passed)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                    # ProjectConfiguration model (extended for symphonies)
├── config_discovery.py          # Config loading & validation (handles backward-compat)
├── daemon.py                    # CoordinareDaemon orchestration loop (per-symphony iteration)
├── dashboard.py                 # Dashboard app (new /symphonies page, API endpoints)
├── observability.py             # bind_symphony(), clear_symphony() context functions (new)
├── metrics.py                   # Prometheus metrics (add symphony label to key metrics)
├── state_store.py               # CoordinareState (extend with symphony_configs, symphony_states)
├── [all other existing modules unchanged]

tests/unit/
├── test_config_symphony.py      # Symphony config parsing, merging, validation
├── test_config_backward_compat.py # Single-project legacy config auto-wrapping
├── test_observability_symphony.py # bind_symphony, clear_symphony context binding
└── test_metrics_symphony.py     # Prometheus label recording per symphony

tests/integration/
├── test_orchestration_multi_symphony.py # Full cycle with multiple boards (mocked GitHub)
├── test_hot_reload.py                  # Config changes detected and applied
├── test_dashboard_api_symphonies.py    # API endpoints for symphony CRUD
└── test_backward_compat_e2e.py         # Existing single-project configs work unchanged
```

**Structure Decision**:
- **No new source files** — extend existing modules (config.py, daemon.py, dashboard.py, observability.py, metrics.py)
- **Core logic**: ~500-700 LOC distributed across existing modules (config merging, per-symphony loop, state tracking)
- **Dashboard**: ~200 LOC (new page registration, API endpoints, form handling)
- **Tests**: ~300-400 LOC across unit and integration tests
- **Configuration**: Extend existing config.yaml schema with `symphonies:` and `orchestra:` keys
- **Backward-compat**: Config discovery auto-wraps legacy single-project configs as implicit `__default__` symphony

**Integration Points**:
- `config.py`: Add `SymphonyConfig`, `OrchestraConfig`, `CoordinareConfiguration` pydantic models; `merge()` function
- `config_discovery.py`: Detect legacy vs. multi-symphony; auto-wrap single-project
- `daemon.py`: Per-symphony loop; bind/clear symphony context; track per-symphony state
- `dashboard.py`: New `/symphonies` page; API endpoints for CRUD; hot-reload trigger
- `observability.py`: `bind_symphony()`, `clear_symphony()` functions
- `metrics.py`: Add `symphony` label to relevant metrics; increment labels per symphony
- `state_store.py`: Extend `CoordinareState` with `symphony_configs`, `symphony_states`, `current_symphony`

## Complexity Tracking

> **Status**: ✅ **No violations** — Constitution check passed. No complexity justifications needed.

All design decisions align with project principles:
- Config merging adds minimal complexity (well-tested pattern in pydantic)
- Per-symphony state tracking is straightforward (dictionary keyed by symphony name)
- Sequential orchestration preferred over concurrent (user confirmed at clarification Q3)
- Backward-compatibility achieved via config discovery (zero breaking changes)
