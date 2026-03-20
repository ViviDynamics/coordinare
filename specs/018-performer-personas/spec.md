# Feature Specification: Performer Personas

**Feature Branch**: `018-performer-personas`
**Created**: 2026-03-17
**Status**: Draft

## Overview

The coordinare orchestrates multiple distinct performer roles across the lifecycle of a card — an implementer that writes code and opens a PR, an assessor that evaluates whether a card is sufficiently specified, and an advocate that discovers new work. Each role is backed by an AI agent, but today all roles run with fixed, hardcoded behavioral instructions. Users have no way to influence how aggressively the assessor asks clarifying questions, what coding style the implementer follows, or how the advocate prioritizes new issues.

This feature introduces **per-role personas**: named, persistent sets of behavioral instructions attached to each coordinare role. Users configure personas through the existing config file or web dashboard; the coordinare injects the relevant persona into each dispatch so the AI agent receives the instructions as part of its working context.

The coordinare's full development lifecycle involves eight AI-backed roles: **advocate**, **assessor**, **architect**, **implementer**, **reviewer**, **security**, **QA**, and **tech writer** (see spec 012). Each role runs on whatever backend agent is declared in configuration — no vendor is hardcoded. Personas are backend-agnostic: the instruction text is plain language that any agent can act on regardless of provider or model. This spec delivers personas for the three currently implemented roles (implementer, assessor, advocate) and defines the persona contract for all remaining roles ahead of their implementation.

## User Scenarios & Testing

### User Story 1 — Configure implementer persona via config file (Priority: P1)

A developer wants the implementer to always write tests first, prefer functional patterns, and add structured logging. They add a `personas.implementer` section to `config.yaml` with a block of custom instructions. On the next card dispatch, the performer receives those instructions alongside the card details and applies them throughout the implementation.

**Why this priority**: The implementer is the highest-value role — it does the actual coding work. Per-role behavioral instructions directly improve output quality and consistency with team standards.

**Independent Test**: Set `personas.implementer.instructions` in config; dispatch a card; assert the dispatch payload received by the performer contains the configured instructions. Deliverable: implementer persona is live end-to-end.

**Acceptance Scenarios**:

1. **Given** `personas.implementer.instructions` is set in `config.yaml`, **When** the coordinare dispatches a card to the implementer, **Then** the dispatch payload includes the configured instructions as a dedicated field.
2. **Given** no `personas.implementer` section exists in config, **When** a card is dispatched, **Then** the implementer receives the built-in default instructions (system behaves identically to today).
3. **Given** `personas.implementer.instructions` is an empty string, **When** a card is dispatched, **Then** the default instructions are used (empty string does not override the default).

---

### User Story 2 — Configure assessor persona via config file (Priority: P2)

A product manager wants the assessor to focus on business value and user impact rather than technical completeness. They configure `personas.assessor.instructions` to guide the AI toward asking business-oriented clarification questions. On the next assess cycle, the assessment service uses those instructions when deciding what questions to ask.

**Why this priority**: The assessor gatekeeps dispatch — its behavior directly determines how often cards get blocked for clarification and what questions are asked. Teams have strong and varied opinions on this threshold.

**Independent Test**: Set `personas.assessor.instructions` in config; trigger an assess cycle; assert the assessment backend is invoked with the configured instructions injected into its prompt context.

**Acceptance Scenarios**:

1. **Given** `personas.assessor.instructions` is configured, **When** `assess_card` runs for a card, **Then** the assessment service receives the configured instructions as part of its evaluation context.
2. **Given** no assessor persona is configured, **When** `assess_card` runs, **Then** the built-in default assessment behavior applies unchanged.

---

### User Story 3 — View and edit personas via web dashboard (Priority: P3)

A team lead wants to review and adjust personas without editing YAML manually. The web dashboard shows the current instructions for each role with an edit field. They update the implementer instructions and click Save; the change takes effect on the next dispatch without restarting the daemon.

**Why this priority**: Config-file editing is sufficient for power users, but a UI lowers the barrier for non-technical team members and makes personas a first-class feature rather than a hidden config knob.

**Independent Test**: Load the dashboard personas page; read the current implementer instructions; submit an edit; verify the next dispatch payload reflects the updated instructions.

**Acceptance Scenarios**:

1. **Given** the dashboard is open, **When** the user navigates to the personas section, **Then** each role's current instructions (or "using defaults" indicator) are displayed.
2. **Given** the user edits and saves a persona, **When** the next card of that role is dispatched, **Then** the updated instructions are used — no daemon restart required.
3. **Given** the user clicks "Reset to defaults" for a role, **When** the next dispatch occurs, **Then** the built-in default instructions are used.

---

### User Story 4 — Configure advocate persona via config file (Priority: P4)

A team lead wants the advocate to focus only on issues labelled `coordinare-ready` and to ignore feature requests tagged `backlog`. They configure `personas.advocate.instructions` with filtering guidance. The advocate applies these instructions when scanning and prioritising issues to add to the board.

**Why this priority**: Valuable but lower urgency than the coding and assessment roles; teams can get initial value from implementer and assessor customization alone.

**Independent Test**: Set `personas.advocate.instructions`; trigger an advocate scan; assert the advocate service receives the configured instructions.

**Acceptance Scenarios**:

1. **Given** `personas.advocate.instructions` is configured, **When** the advocate scans GitHub issues, **Then** it receives the configured instructions as context for prioritisation decisions.
2. **Given** no advocate persona is configured, **When** the advocate scans, **Then** the built-in default scanning behavior applies.

---

### Edge Cases

- What happens if `personas.implementer.instructions` is extremely long (e.g. 50,000 characters)? The system enforces a fixed maximum length of 8,000 characters; instructions exceeding this limit are rejected with a clear error message (HTTP 400 from the API, `ValidationError` at config-load time). The active persona is unchanged when a write is rejected.
- What if the config file is edited while the daemon is running? Changes take effect on the next dispatch cycle without restart (hot-reload on each invocation).
- What if a persona references unavailable capabilities? The persona is still injected — the agent's own error handling applies; the coordinare does not validate instruction content.
- What if the dashboard save fails (e.g. disk full)? The old persona remains active; an error is shown to the user.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST support a persona definition for each coordinare role: `implementer`, `assessor`, `advocate`, `architect`, `reviewer`, `security`, `QA`, and `tech writer`. Personas for roles not yet implemented are defined and stored now; they will be injected into dispatches once those roles ship (see spec 012).
- **FR-002**: Each persona MUST consist of at minimum an `instructions` field: a freeform text block of behavioral guidance for the AI agent.
- **FR-003**: Persona instructions MUST be persisted across daemon restarts (stored in config file or a dedicated personas file alongside config).
- **FR-004**: The implementer persona instructions MUST be injected into every performer dispatch payload as a dedicated field so the backend agent receives them without the coordinare interpreting them.
- **FR-005**: The assessor persona instructions MUST be passed into the assessment service evaluation context on every `assess_card` invocation.
- **FR-006**: The advocate persona instructions MUST be passed into the advocate service on every issue-scan invocation.
- **FR-007**: When no persona is configured for a role (or instructions are empty/whitespace), the system MUST use built-in default instructions for that role; the defaults MUST be documented.
- **FR-008**: Persona configuration MUST be editable via the existing `config.yaml` without requiring a daemon restart (hot-reload within one poll cycle).
- **FR-009**: The web dashboard MUST display the current effective instructions for each role (configured or default) and allow inline editing and saving.
- **FR-010**: The web dashboard MUST provide a "Reset to defaults" action per role that removes any custom instructions and restores the built-in defaults.
- **FR-011**: Persona instructions MUST be capped at a maximum length (default: 8,000 characters); instructions exceeding this limit MUST be rejected with a clear error message.

### Key Entities

- **Persona**: A named configuration object scoped to a single coordinare role. Has: `role` (advocate | assessor | architect | implementer | reviewer | security | QA | tech writer), `instructions` (freeform text). Each role has exactly one active persona at any time.
- **Persona Store**: The persistence layer for all personas. Backed by the config file or a sidecar file; exposes read/write/reset operations used by both the coordinare daemon and the dashboard API.
- **Default Instructions**: The built-in fallback instructions per role, defined in code. Applied whenever a role's persona has no custom instructions.

## Success Criteria

### Measurable Outcomes

- **SC-001**: A user can configure a custom implementer persona and have it reflected in the next dispatch within one poll cycle (≤ 30 seconds after saving), with no daemon restart.
- **SC-002**: 100% of card dispatches include the effective persona instructions (custom or default) — zero dispatches omit the field when a persona is configured.
- **SC-003**: Dashboard persona editing round-trips in under 2 seconds from save to confirmation.
- **SC-004**: All three role personas are independently configurable — changing one does not affect the others.
- **SC-005**: Default instructions produce behavior equivalent to today's (no regression for teams that do not configure personas).

## Assumptions

- All eight roles (`advocate`, `assessor`, `architect`, `implementer`, `reviewer`, `security`, `QA`, `tech writer`) are supported in the persona store from the start, even if not all roles are yet implemented. Personas for unimplemented roles are stored but not yet injected.
- "Hot-reload" means the coordinare reads persona config on each dispatch/assess/scan invocation rather than caching at startup — no file-watching mechanism is required.
- Per-card persona overrides are explicitly out of scope; personas are role-level only.
- Personas are backend-agnostic — the instruction text is plain language and makes no assumptions about which AI provider or model is running a given role. The same persona works regardless of which configured agent executes it.
- The receiving agent is responsible for interpreting and acting on the persona instructions; the coordinare injects them verbatim and does not validate or interpret the content.
- Persona storage will use the existing config infrastructure (YAML file) rather than a database; no new persistence backend is introduced.

## Out of Scope

- Training or fine-tuning AI models
- Changing which AI backend is used per role (e.g. switching implementer from OpenCode to another agent)
- Per-card or per-project persona overrides
- Role-specific tool or capability restrictions enforced by the coordinare
- Version history or rollback of persona changes
