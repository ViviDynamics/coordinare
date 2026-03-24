# Feature Specification: Per-Role Model Selection

**Feature Branch**: `037-per-role-model-selection`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare's `PerformerRoleConfig` already includes a `backend` field (e.g. `opencode`, `claude_code`, `codex`), but this value is not passed to the performer at dispatch time. The performer always uses its own `AGENT_BACKEND` environment variable default. This feature wires the per-role `backend` field through the dispatch payload so operators can assign different AI backends and models to different performer roles — for example, a cheaper model for documentation and a stronger model for implementation.

## Clarifications

### Session 2026-03-24

- Q: Does the performer already read a backend override from the dispatch payload? → A: No. The performer reads `AGENT_BACKEND` from its own environment. The coordinare dispatch payload does not include backend information.
- Q: Should `model` be a free-form string or validated against a known list? → A: Free-form string. The performer backend adapters are responsible for interpreting the model name. Coordinare does not validate it.
- Q: What happens if `backend` is set to a value the performer doesn't support? → A: The performer raises `UnsupportedBackendError` at startup. The coordinare sees this as an error status and moves the card to Blocked.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Backend Override via Dispatch Payload (Priority: P1)

An operator configures `performers.implementer.backend: claude_code` and `performers.tech_writer.backend: opencode` in `config.yaml`. When the coordinare dispatches the implementer, the performer receives `backend: "claude_code"` in the dispatch payload and uses it instead of its `AGENT_BACKEND` default. When the tech writer is dispatched, it receives `backend: "opencode"`.

**Why this priority**: The `backend` field already exists in config but has no runtime effect. Wiring it through is the minimal viable change.

**Independent Test**: Dispatch a performer with a stub transport, capture the dispatch payload, and verify the `backend` field matches the role config.

**Acceptance Scenarios**:

1. **Given** `performers.implementer.backend` is `"claude_code"`, **When** the coordinare dispatches the implementer, **Then** the dispatch payload includes `"backend": "claude_code"`.
2. **Given** the performer receives a dispatch payload with `"backend": "claude_code"`, **When** it initializes, **Then** it calls `get_backend("claude_code")` instead of using `AGENT_BACKEND`.
3. **Given** `performers.reviewer.backend` is not set (defaults to `"opencode"`), **When** the coordinare dispatches the reviewer, **Then** the dispatch payload includes `"backend": "opencode"`.

---

### User Story 2 — Model Selection Within a Backend (Priority: P2)

An operator configures `performers.implementer.model: claude-sonnet-4-20250514` in addition to `backend: claude_code`. The performer passes this model identifier to the backend adapter, which uses it for API calls. This allows selecting specific model versions within a backend family.

**Why this priority**: Model selection is a refinement of backend selection. It is useful for cost optimization but not required for basic multi-backend operation.

**Independent Test**: Dispatch a performer with `model` in the payload, and verify the backend adapter receives the model name.

**Acceptance Scenarios**:

1. **Given** `performers.implementer.model` is `"claude-sonnet-4-20250514"`, **When** the performer initializes the backend, **Then** the backend adapter's `start()` receives the model name.
2. **Given** `model` is not set for a role, **When** the performer initializes, **Then** the backend uses its own default model.
3. **Given** `model` is set but `backend` is not, **When** the performer initializes, **Then** the default backend (`opencode`) is used with the specified model.

---

### Edge Cases

- What if the backend name in config doesn't match any registered backend adapter? The performer raises `UnsupportedBackendError`, which surfaces as a dispatch failure.
- What if the model name is invalid for the chosen backend? The backend adapter raises an error during `start()`, which the performer reports as an error status.
- What if the operator changes backend config while a performer is running? The running performer is unaffected; the new config applies to the next dispatch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare `dispatch_performer` node MUST include the role's `backend` field in the dispatch payload sent to the performer.
- **FR-002**: The coordinare MUST include the role's `model` field in the dispatch payload when it is set (non-None).
- **FR-003**: The performer MUST read `backend` from the dispatch payload and use it to select the backend adapter via `get_backend()`, overriding `AGENT_BACKEND`.
- **FR-004**: The performer MUST read `model` from the dispatch payload (when present) and pass it to the backend adapter's `start()` method.
- **FR-005**: `PerformerRoleConfig` MUST include an optional `model: str | None = None` field.
- **FR-006**: When `backend` is absent from the dispatch payload, the performer MUST fall back to `AGENT_BACKEND` for backward compatibility.
- **FR-007**: When `model` is absent from the dispatch payload, the backend adapter MUST use its own default model.

### Key Entities

- **PerformerRoleConfig.backend**: Existing field (`str`, default `"opencode"`). Not yet wired to dispatch.
- **PerformerRoleConfig.model**: New optional field (`str | None`, default `None`). Specifies model within backend.
- **Dispatch payload**: JSON object sent via transport to the performer. Will gain `backend` and `model` keys.
- **BackendAdapter.start()**: Existing method. Will accept optional `model` parameter.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Two performer roles configured with different backends use different backend adapters at runtime.
- **SC-002**: A performer dispatched with `model: "claude-sonnet-4-20250514"` passes that model name to the backend adapter.
- **SC-003**: Existing deployments with no per-role backend config behave identically to today (backward compatible).
- **SC-004**: An unsupported backend name in config results in a clear error status on the card, not a crash.

## Assumptions

- The performer subprocess inherits environment variables from the coordinare. Backend override via dispatch payload takes precedence over environment.
- Backend adapters that do not support model selection silently ignore the `model` parameter (no error for unused models).
- The dispatch payload format is stable and backward-compatible: adding new optional keys does not break existing performers.
