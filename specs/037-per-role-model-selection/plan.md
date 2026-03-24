# Implementation Plan: Per-Role Model Selection

**Branch**: `037-per-role-model-selection` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Wire the existing `PerformerRoleConfig.backend` field through the coordinare dispatch payload to the performer, and add a new optional `model` field for model-level selection within a backend. The performer reads these from the dispatch payload and overrides its `AGENT_BACKEND` default. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic, structlog (all existing) — **no new dependencies**
**Storage**: Existing `config.yaml` — `performers.<role>.model` added as optional field
**Constraints**: Backward compatible; performers without payload backend field work unchanged
**Scale/Scope**: ~3 modified coordinare files, ~2 modified performer files, ~8 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Wires existing config field to runtime; no dead-code addition |
| II. Testing Discipline | PASS | Each dispatch path and fallback tested independently |
| III. User Experience | PASS | Zero-config for existing users; opt-in per-role override |
| IV. Performance by Design | PASS | Resolution at dispatch time only |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                          # MODIFIED — add model field to PerformerRoleConfig
src/coordinare/graph/nodes/dispatch_performer.py  # MODIFIED — include backend and model in payload
agent/performer/src/performer/main.py            # MODIFIED — read backend/model from dispatch payload
agent/performer/src/performer/protocol.py        # MODIFIED — add model field to DispatchResponse
agent/performer/src/performer/backends/base.py   # MODIFIED — add optional model param to start()
tests/unit/graph/nodes/test_dispatch_performer.py # MODIFIED — verify backend/model in payload
tests/unit/test_config.py                        # MODIFIED — test model field parsing
```

## Detailed Implementation Plan

### Step 1 — Add `model` Field to PerformerRoleConfig (`src/coordinare/config.py`)

Add `model: str | None = None` to `PerformerRoleConfig`. No validation — free-form string interpreted by the backend adapter.

### Step 2 — Include `backend` and `model` in Dispatch Payload

In `dispatch_performer.py`, read the current role's config and include `payload["backend"] = role_config.backend` and conditionally `payload["model"] = role_config.model`. Requires the dispatch node to access the `PerformerRoleConfig` for the current stage via the service wrapper or state metadata.

### Step 3 — Performer Reads Backend/Model from Dispatch Payload

In `performer/main.py`, the `dispatch()` handler reads `backend_name = payload.get("backend") or settings.AGENT_BACKEND` and `model_name = payload.get("model")`, then calls `get_backend(backend_name)` and passes `model` to `backend.start()`.

### Step 4 — Extend BackendAdapter.start() Signature

Add `*, model: str | None = None` to `BackendAdapter.start()`. Each concrete adapter (OpenCode, ClaudeCode, Codex) accepts `model` and uses it if provided, falling back to its own default.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| 1 new config field | ~3 LOC | Minimal addition to existing model |
| Dispatch payload change | ~8 LOC | Two new keys in existing payload dict |
| Performer backend override | ~10 LOC | Read from payload, pass to adapter |
| BackendAdapter signature | ~5 LOC per adapter | Add optional kwarg to existing method |
