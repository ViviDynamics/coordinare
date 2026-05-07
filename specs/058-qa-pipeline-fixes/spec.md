# Feature Specification: QA Pipeline Fixes

**Feature Branch**: `058-qa-pipeline-fixes`
**Created**: 2026-05-04
**Status**: Draft — actively collecting QA findings
**Input**: User description: "QA-driven improvements from live testing of coordinare. First confirmed finding: sessions in monitoring_pr phase (awaiting human review / CI) should not count toward the max_concurrent_cards concurrency limit — coordinare should be able to pick up new TODO cards while existing cards wait for human review. More QA findings will be added to this spec as testing continues."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Cards in Human Review Don't Block New Work (Priority: P1)

A coordinare operator has configured `max_concurrent_cards: 3`. Two cards are sitting in the IN_REVIEW column waiting for human approval — a process that can take hours. The third slot is idle. Coordinare should detect the idle capacity and pick up the next TODO card, rather than treating the two waiting-for-review cards as consuming active slots.

**Why this priority**: Human review is a passive wait — coordinare isn't doing any work on those cards. Blocking the entire pipeline on reviewer availability defeats the purpose of concurrency and was the original motivation for the `max_concurrent_cards` feature.

**Independent Test**: Can be tested by creating a board with 3 TODO cards, setting `max_concurrent_cards: 2`, advancing one card to IN_REVIEW (awaiting human approval), then confirming coordinare picks up a second TODO card rather than sitting idle.

**Acceptance Scenarios**:

1. **Given** `max_concurrent_cards: 2` and one session passively awaiting human review, **When** coordinare polls and sees an eligible TODO card, **Then** it picks up the TODO card (treating the review-waiting session as a free slot).
2. **Given** all sessions are actively working (dispatching, running CI, merging), **When** coordinare polls, **Then** it does not pick up additional cards beyond `max_concurrent_cards`.
3. **Given** a session transitions from passive review-waiting back to active work (e.g., reviewer requests changes), **When** coordinare counts active slots, **Then** that session counts again toward the limit.

---

### User Story 2 — Performer Backend Inherited Correctly Through Symphony Config (Priority: P1)

An operator configures a `default` performer with `backend: codex` and defines several roles (assessor, architect, implementer, etc.) without explicitly repeating the backend — expecting all roles to inherit `codex` from the default. When coordinare runs with symphonies configured, all performer dispatches should use `codex`, not silently fall back to a different backend.

**Why this priority**: A silent backend substitution means every dispatched card runs against the wrong agent backend with no warning in logs or the dashboard. This is a correctness bug with direct impact on every card in flight.

**Independent Test**: Configure `default.backend: codex` with no `backend` field on any other role, add at least one symphony, dispatch a card, and confirm the job payload received by the performer specifies `codex` — not `opencode` or any other backend.

**Acceptance Scenarios**:

1. **Given** `performers.default.backend: codex` and no explicit `backend` on any role, **When** coordinare dispatches any role through a symphony's effective config, **Then** the job payload carries `backend: codex` for every role.
2. **Given** a role explicitly sets `backend: opencode`, **When** coordinare dispatches that role, **Then** the job payload carries `backend: opencode` (explicit override is respected).
3. **Given** no `backend` is set anywhere in config (all defaults), **When** coordinare dispatches any role, **Then** the system behaves consistently — the same backend is used regardless of whether symphonies are configured.

---

### User Story 3 — Symphony Configuration Editable via Dashboard (Priority: P2)

An operator needs to adjust a symphony's settings (concurrency limit, poll interval, assignee filter, per-role persona instructions) without manually editing `config.yaml` and restarting coordinare. The Symphonies page in the dashboard should provide a web form that reads the current config, lets the operator edit it, and saves changes immediately — reloading coordinare's in-memory config without a restart.

**Why this priority**: The dashboard is the primary operator interface. A read-only view of symphonies that requires SSH access and a text editor to change anything makes the dashboard incomplete for day-to-day operations.

**Independent Test**: Navigate to `/symphonies/{name}` in the dashboard, change `max_concurrent_cards`, click Save, and confirm the value is reflected in `config.yaml` and the next coordinare poll respects the new limit — all without restarting coordinare.

**Acceptance Scenarios**:

1. **Given** a running coordinare with at least one symphony, **When** an operator navigates to `/symphonies/{name}`, **Then** the page shows a form pre-populated with the symphony's current `enabled` state, override fields, and per-role persona instructions.
2. **Given** an operator changes a field and clicks Save, **When** the PUT request completes, **Then** `config.yaml` reflects the change and coordinare's in-memory config is updated within one poll cycle (no restart required).
3. **Given** an operator fills in the Add Symphony form (name + GitHub project number) and submits, **When** the POST request completes, **Then** the new symphony appears in `config.yaml` and the coordinare begins polling it without a restart.
4. **Given** an operator clicks Delete on a symphony detail page, **When** confirmation is accepted and the DELETE completes, **Then** the symphony is removed from `config.yaml` and the dashboard returns to the list page.

---

### Edge Cases

- What happens when all sessions are passively awaiting human review? Coordinare should pick up new TODO cards up to the full `max_concurrent_cards` limit.
- What happens if `max_concurrent_cards` is 1 and the single session is awaiting human review? Coordinare should still pick up a new card.
- What happens when a card moves from review-waiting back to active (reviewer requests changes)? That session must immediately count toward the concurrency limit on the next poll cycle.
- What if the board has no eligible TODO cards when review-waiting slots free up? No action needed — normal idle behavior.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The concurrency limit (`max_concurrent_cards`) MUST only count sessions that are actively being worked on — sessions passively waiting for human review MUST NOT consume a concurrency slot.
- **FR-002**: The system MUST enforce the concurrency limit consistently at both the card pickup stage and the symphony-level capacity check.
- **FR-003**: When a session transitions out of passive waiting (e.g., reviewer requests changes, PR is closed), it MUST immediately count toward the concurrency limit on the next poll cycle.
- **FR-004**: The system MUST pick up eligible TODO cards whenever active (non-review-waiting) session count is below `max_concurrent_cards`, even if total session count equals or exceeds the limit.
- **FR-005**: All existing concurrency behavior for non-review-waiting phases (dispatching, CI monitoring, merging, relay_feedback, blocked, recovery) MUST remain unchanged.
- **FR-006**: The system MUST correctly propagate the `default` performer's `backend` setting to all roles that do not explicitly override it, even when role configs are merged through the symphony effective-config path.
- **FR-007**: A role's inherited `backend` value MUST be indistinguishable in behavior from a role where `backend` is set explicitly to the same value — no silent substitution may occur at any point in the config resolution chain.
- **FR-008**: The dashboard `GET /api/symphonies/{name}` endpoint MUST return the symphony's `enabled` flag, `overrides` dict, and `personas` dict so the edit form can pre-populate.
- **FR-009**: A `POST /api/symphonies` endpoint MUST accept `{name, github_project_number}`, validate, persist to `config.yaml`, and trigger a hot-reload — returning 409 if the symphony already exists.
- **FR-010**: The `PUT /api/symphonies/{name}` endpoint MUST accept and persist the `enabled` field alongside `overrides` and `personas`, then trigger a hot-reload.
- **FR-011**: All symphony mutations (create, update, delete) MUST persist atomically to `config.yaml` and trigger coordinare's in-memory hot-reload without requiring a process restart.

### Key Entities

- **Active Session**: A card session performing work coordinare controls — dispatching an agent, monitoring CI, executing a merge, relaying feedback. Counts toward `max_concurrent_cards`.
- **Review-Waiting Session**: A card session in a passive state where coordinare is waiting for a human to take action (approve, request changes, close). Does NOT count toward `max_concurrent_cards`.
- **Concurrency Slot**: One unit of the `max_concurrent_cards` budget. Consumed only by active sessions.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With `max_concurrent_cards: N` and K sessions passively awaiting human review (K < N), coordinare picks up `N - K` additional TODO cards within one poll cycle.
- **SC-002**: No regression in concurrency behavior — coordinare never dispatches more than `max_concurrent_cards` active (non-review-waiting) sessions simultaneously.
- **SC-003**: When a review-waiting session transitions back to active, the concurrency limit is enforced correctly on the next poll cycle without a restart.
- **SC-004**: All existing tests continue to pass; new tests cover the review-waiting exclusion at both the slot-calculation and symphony-level capacity guard code paths.
- **SC-005**: In a symphony-configured setup, every role dispatched without an explicit `backend` field carries the same backend value as `performers.default.backend` — verified by inspecting the job payload in tests.
- **SC-006**: No silent backend substitution occurs at any log level — if a backend mismatch were to occur, it would be detectable without inspecting the job payload directly.
- **SC-007**: The Symphonies detail page (`/symphonies/{name}`) displays a pre-populated edit form; saving a change updates `config.yaml` within one request cycle.
- **SC-008**: A new symphony created via the dashboard Add Symphony form appears in `config.yaml` and is polled by coordinare without a restart.
- **SC-009**: All existing dashboard and API tests continue to pass after the symphony edit UI additions.

## Assumptions

- "Passively awaiting human review" is defined as sessions in the `monitoring_pr` phase. Other phases (`blocked`, `recovery`, etc.) continue to count toward the limit, as coordinare may take action on them.
- The `max_concurrent_cards` setting value itself is unchanged — only the counting logic changes.
- This behavior applies uniformly across all symphonies; no per-symphony opt-out is needed.
- The backend inheritance bug affects any setup using the `symphonies:` config key; single-project (legacy) configs are not affected since they bypass `effective_config()`.
- The root cause is that `effective_config()` round-trips config through `model_dump()` + re-instantiation, which loses `model_fields_set` information needed by `resolved_role()` to distinguish explicit overrides from defaults.
