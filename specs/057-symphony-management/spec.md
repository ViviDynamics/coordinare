# Feature Specification: Symphony Management & Multi-Project Orchestration

**Feature Branch**: `057-symphony-management`  
**Created**: 2026-04-30  
**Status**: Draft  

## Overview

Coordinare currently manages a single GitHub project board (one "symphony") per running instance. This feature introduces the concept of **symphonies** — named, independently-configured projects that coordinare monitors and orchestrates simultaneously — and an **orchestra** — the shared pool of performers available to work across all symphonies. Each symphony maps to a GitHub Project V2 board and its associated repository. Coordinares manages which performer is assigned to which card in which symphony at any given time.

A **global (default) symphony configuration** provides baseline values inherited by every defined symphony. Individual symphonies may override most settings; a small set of system-level fields remain global-only and cannot be overridden per symphony. Personas for performers are configurable at both the global and per-symphony level.

Configuration is managed via a dedicated dashboard page and supports environment variable substitution (`${VAR}` placeholders) in all string fields. Changes made through the dashboard are validated, persisted to disk, and applied via hot-reload without restarting coordinare.

---

## Clarifications

### Session 2026-04-30

- Q: Should the `symphonies` config key be a named map (key = symphony id) or an ordered list with an explicit `name` field? → A: Ordered list with explicit `name` field, so that list order can serve as an implicit priority for performer allocation across symphonies.
- Q: Should there be a maximum number of configurable symphonies? → A: Soft limit of 10 — coordinare emits a validation warning (not error) when more than 10 symphonies are configured, but continues to operate.
- Q: Should coordinare poll all enabled symphonies concurrently or sequentially within each cycle? → A: Sequential, in list order — avoids bursting the GitHub API since the 30-second poll interval makes sequential polling imperceptible in practice.
- Q: When a symphony is removed, what happens to in-flight performer sessions for that symphony? → A: Cancel immediately — active sessions are terminated when the remove action is saved, so the dashboard remove action is fully resolved within a single request/response cycle.
- Q: Should observability (logging and metrics) be scoped per symphony? → A: Full per-symphony metrics + log tagging — all log lines include a `symphony` field, and all existing prometheus counters/gauges gain a `symphony` label so metrics can be isolated per symphony in monitoring tools.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator Runs Multiple Projects Simultaneously (Priority: P1)

An operator has two GitHub projects (e.g., a backend API repo and a frontend app repo) and wants coordinare to drive development across both using the same pool of performers. They define two symphonies in `config.yaml`, each pointing to a different org/repo/project board, with the same performers serving either. Coordinare polls all active symphonies each cycle, selects eligible cards from any symphony, dispatches the appropriate performer role, and tracks which performer is working on which card in which symphony.

**Why this priority**: This is the core value proposition. Everything else in the feature depends on being able to run multiple symphonies simultaneously.

**Independent Test**: Configure two symphonies with distinct GitHub project boards. Verify coordinare polls both, dispatches a performer to each, and correctly tracks per-symphony card state — all without requiring dashboard UI.

**Acceptance Scenarios**:

1. **Given** two symphonies are defined in config, **When** coordinare starts, **Then** it polls all active symphonies every cycle and dispatches performers to eligible cards across both.
2. **Given** a performer finishes a card in Symphony A, **When** coordinare polls next cycle, **Then** the performer becomes available and may be dispatched to an eligible card in Symphony A or Symphony B.
3. **Given** a symphony is set to `enabled: false`, **When** coordinare runs, **Then** no cards from that symphony are dispatched and no poll occurs for it.
4. **Given** a symphony inherits global config values, **When** an operator sets only the required fields (`project_name`, `github_org`, `github_project_number`, `github_token`), **Then** all other settings resolve from the global defaults.
5. **Given** a per-symphony value is set for an inheritable field (e.g., `assignee_filter`), **When** coordinare resolves effective config for that symphony, **Then** the per-symphony value takes precedence over the global default.

---

### User Story 2 — Operator Manages Symphony Configuration via Dashboard (Priority: P2)

An operator wants to add a new symphony, modify global defaults, or remove a symphony without manually editing `config.yaml`. They navigate to the **Symphonies** page in the coordinare dashboard, view/edit the global default config at the top, and manage the list of individual symphonies (add, remove, edit) in the section below. They click **Validate & Save**, coordinare validates the new config, writes it to disk, and hot-reloads its orchestration state — all without a coordinare restart.

**Why this priority**: Without a UI, multi-symphony management requires hand-editing YAML and restarting coordinare — error-prone and operationally risky.

**Independent Test**: Open the Symphonies page; add a new symphony with required fields; click Validate & Save. Verify the config file is updated on disk and coordinare begins polling the new symphony within one cycle, without restarting.

**Acceptance Scenarios**:

1. **Given** the Symphonies page is open, **When** it loads, **Then** the global default config fields are shown at the top and all configured symphonies are listed below with their overridden values.
2. **Given** an operator fills required fields for a new symphony and clicks Add, **When** they click Validate & Save, **Then** the new symphony appears in the list, the config file is updated, and coordinare starts polling it.
3. **Given** an operator removes an existing symphony and saves, **When** coordinare processes the removal, **Then** active sessions for that symphony are cancelled immediately and the dashboard save completes within a single request/response cycle; cards from that symphony are no longer dispatched on subsequent cycles.
4. **Given** a config field contains a `${VAR}` placeholder, **When** the page renders that field, **Then** the placeholder is displayed as-is (not resolved) so the operator can see and edit the raw value.
5. **Given** an operator enters an invalid value (e.g., a non-numeric port, malformed GitHub project number), **When** they click Validate & Save, **Then** an inline validation error is shown next to the offending field and no write or reload occurs.
6. **Given** an operator sets a global-only field (e.g., `poll_interval_seconds`) at the per-symphony level, **When** they attempt to save, **Then** the dashboard rejects the change with a clear explanation that the field is global-only.

---

### User Story 3 — Per-Symphony Persona Customization (Priority: P3)

An operator has a frontend symphony where they want the implementer persona to include framework-specific instructions not relevant to their backend symphony. They navigate to the per-symphony config section, expand the **Personas** accordion, and override just the `implementer` persona. The backend symphony continues to use the global default personas unchanged.

**Why this priority**: Persona customization is high-value for domain-specific projects but is not required for the core multi-symphony orchestration to function.

**Independent Test**: Set a per-symphony implementer persona override. Verify that when a performer is dispatched for a card in that symphony, it receives the overridden persona — while a card in another symphony uses the global default.

**Acceptance Scenarios**:

1. **Given** a symphony defines a `personas.implementer` override, **When** an implementer is dispatched for a card in that symphony, **Then** the performer receives the overridden instructions, not the global default.
2. **Given** a symphony does not define any persona overrides, **When** a performer is dispatched, **Then** it receives the global default personas.
3. **Given** the Symphonies page shows a symphony's persona section, **When** an operator edits only one role's persona, **Then** only that role is persisted as an override; all others remain inherited.

---

### User Story 4 — Hot-Reload Without Restart (Priority: P4)

An operator makes a config change (adding a symphony, adjusting an existing symphony's `assignee_filter`) through the dashboard and saves. Coordinare detects the change, validates it, and applies it to the running orchestration loop — without stopping or restarting the process. In-flight performer sessions are not interrupted; only the next dispatch cycle reflects the new config.

**Why this priority**: Downtime-free configuration changes are important for production use but less critical than the ability to configure multiple symphonies in the first place.

**Independent Test**: With coordinare running and a performer active on a card, save a config change via the dashboard. Verify the running performer session is not interrupted and the new config is applied on the next cycle.

**Acceptance Scenarios**:

1. **Given** coordinare is running with an active performer session on a symphony whose settings are modified, **When** a valid config change is saved, **Then** the active session continues uninterrupted and the new config takes effect on the next poll cycle.
2. **Given** coordinare is running with an active performer session on a symphony that is removed, **When** the remove action is saved, **Then** the active session is cancelled immediately and the dashboard save completes within a single request/response cycle.
3. **Given** a hot-reload is triggered with an invalid config, **When** the validation fails, **Then** coordinare continues running with the previous valid config and logs a warning; no partial state is applied.
4. **Given** a new symphony is added via hot-reload, **When** the next poll cycle runs, **Then** coordinare begins polling the new symphony as though it had always been configured.

---

### Edge Cases

- What happens when two symphonies point to the same GitHub project board? Coordinare must detect and reject duplicate `(github_org, github_project_number)` pairs at validation time.
- What happens when a symphony's required credentials (e.g., `github_token`) are invalid? Coordinare logs an error per symphony and skips that symphony's poll cycle without affecting others.
- What happens when all performers are busy across all symphonies and a new card becomes eligible? Coordinare queues it for the next available performer slot, same as today.
- What happens when a symphony is removed while a performer is actively working a card in it? All in-flight sessions for that symphony are cancelled immediately as part of the remove action, so the dashboard remove completes within a single request/response cycle. Affected cards are left in their last-known GitHub state (no cleanup commit or PR closure is performed).
- What happens when `${VAR}` placeholders remain unresolved at runtime (env var not set)? Coordinare surfaces a per-symphony config error and skips that symphony's dispatch; other symphonies are unaffected.
- What happens when the config file is externally modified while coordinare is running? Out of scope for this iteration — external file modification detection is deferred to a future feature. Operators must use the dashboard save action or restart coordinare to apply manual config file edits.
- What happens when no symphonies are explicitly defined? Coordinare falls back to treating the global config as a single implicit symphony (backwards compatibility with existing `config.yaml` files).

---

## Requirements *(mandatory)*

### Functional Requirements

**Config Schema**

- **FR-001**: The config schema MUST introduce a `symphonies` top-level key containing an **ordered list** of symphony configuration objects, each with an explicit `name` field; an empty or absent `symphonies` key means coordinare operates in single-symphony mode using the global config as the sole symphony. List order is the canonical priority: when multiple symphonies have eligible cards and performers are scarce, symphonies earlier in the list are favored for dispatch.
- **FR-002**: The global (default) symphony configuration MUST expose all `ProjectConfiguration` fields. Any field may be overridden per symphony via the symphony's `overrides` dict; the effective config is produced by merging global defaults with symphony-level overrides (symphony values take precedence). Fields such as `agent_transport`, `agent_executable`, `poll_interval_seconds`, `health_check_port`, `dashboard_port`, `output_mode`, `log_level`, and `notifications` are typically set once globally, but the schema does not prevent per-symphony overrides if an operator explicitly sets them.
- **FR-003**: Each symphony's effective configuration MUST be resolved by merging the global defaults with the symphony's explicit overrides (symphony values take precedence over global defaults at the field level).
- **FR-004**: Persona configuration (`personas.*`) MUST be overridable per symphony at the individual role level (e.g., overriding `personas.implementer` does not affect `personas.reviewer`).
- **FR-005**: All string-typed config fields in both global and per-symphony sections MUST support `${ENV_VAR}` substitution; substitution MUST be performed at load time using the process environment.
- **FR-006**: Each symphony MUST have a unique `name` field (string, slug-safe) used to namespace its state, logs, and dashboard representation; duplicate `name` values within the list MUST be rejected at validation time.
- **FR-007**: Duplicate `(github_org, github_project_number)` pairs across symphonies MUST be rejected at validation time with a descriptive error.
- **FR-007a**: The dashboard Symphonies page MUST allow operators to reorder symphonies in the list (e.g., via drag-and-drop or explicit up/down controls), with the saved order reflected in config and used for performer dispatch priority.
- **FR-008**: Each symphony MUST support an `enabled` boolean field (default: `true`) that controls whether coordinare polls and dispatches for it.
- **FR-008a**: Coordinare MUST emit a validation warning (non-fatal) when more than 10 symphonies are configured; all symphonies are still loaded and operated. The warning is surfaced in logs and on the Symphonies dashboard page.

**Orchestration**

- **FR-009**: Coordinare MUST poll all enabled symphonies during every cycle and maintain independent card state per symphony. Symphonies are polled **sequentially in list order** within each cycle to avoid bursting the GitHub API.
- **FR-010**: The performer pool (orchestra) MUST be shared across all symphonies; any performer may be dispatched to any symphony's card within the performer's configured roles.
- **FR-011**: Coordinare MUST track and enforce `max_concurrent_cards` limits independently per symphony.
- **FR-012**: Session tracking MUST record which symphony a performer session belongs to, so the dashboard can display per-symphony activity.
- **FR-012a**: All structured log lines emitted by coordinare MUST include a `symphony` field (the symphony `name`) when the log event is scoped to a specific symphony.
- **FR-012b**: All existing prometheus metrics counters and gauges MUST gain a `symphony` label so operators can isolate per-symphony metrics in monitoring tools (e.g., `cards_dispatched_total{symphony="frontend"}`). Global aggregates (unlabeled) are preserved for backwards compatibility.

**Hot-Reload**

- **FR-013**: Coordinare MUST support hot-reload of symphony configuration without process restart; the reload MUST be triggerable via the dashboard save action. External file modification detection (inotify/polling) is out of scope for this iteration and may be added in a future feature.
- **FR-014**: Hot-reload MUST validate the new configuration before applying it; if validation fails, coordinare MUST retain the current running configuration and surface the validation errors.
- **FR-015**: In-flight performer sessions MUST NOT be interrupted during a hot-reload of a symphony's settings (e.g., changing `assignee_filter`); the new config takes effect on the next dispatch cycle. Exception: when a symphony is **removed**, all its active sessions MUST be cancelled immediately as part of the save action, completing within the same request/response cycle that triggered the removal.

**Dashboard UI**

- **FR-016**: The dashboard MUST include a dedicated **Symphonies** page accessible from the main navigation.
- **FR-017**: The Symphonies page MUST display the global default configuration fields at the top, grouped logically (GitHub credentials, performer settings, personas, etc.), with editable inputs.
- **FR-018**: The Symphonies page MUST display the list of configured symphonies below the global section, each with its name, enabled status, and overridden fields; non-overridden fields MUST show the inherited global value as a placeholder or grayed-out default.
- **FR-019**: The Symphonies page MUST provide **Add Symphony** and **Remove Symphony** controls; adding a symphony shows only required fields by default, with optional fields collapsible.
- **FR-020**: The Symphonies page MUST provide a **Validate & Save** action that: (1) validates the full composed config, (2) shows inline errors next to offending fields, (3) writes to `config.yaml` on success, (4) triggers hot-reload.
- **FR-021**: The Symphonies page MUST render `${VAR}` placeholder values as raw text (not resolved), allowing operators to see and edit the substitution expressions directly.
- **FR-022**: The dashboard MUST show per-symphony activity in the main orchestration view (card state, active sessions) labeled with the symphony name.

### Key Entities

- **Symphony**: A named, independently-configured project instance pointing to a specific GitHub org, project board, and repository. Defined as a list item with a required `name` field (unique slug). Inherits all overridable fields from the global default; required fields are `name`, `project_name`, `github_org`, `github_project_number`, and authentication credentials. Has an `enabled` flag. List position determines dispatch priority when performers are scarce.
- **Global Default Config**: The base `ProjectConfiguration` values applied to all symphonies. Contains all fields except the global-only set. Acts as the single implicit symphony when no `symphonies` key is present (backwards compatibility).
- **Orchestra**: The shared pool of performers (performer endpoints / subprocess slots) available to work across all symphonies. Not replicated per symphony.
- **Effective Symphony Config**: The resolved configuration for a given symphony, produced by merging global defaults with that symphony's explicit overrides. This is what coordinare uses at runtime.
- **Symphony State**: Per-symphony runtime state tracking: active cards, in-flight sessions, cycle counts, last poll timestamp, and card history.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can configure two independent GitHub project boards and have coordinare orchestrate both simultaneously within 5 minutes of editing `config.yaml`, without any code changes.
- **SC-002**: A symphony configuration change (add, modify, remove) made through the dashboard takes effect within one poll cycle of saving, without a coordinare restart.
- **SC-003**: Invalid configuration (duplicate boards, missing required fields, unresolvable placeholders) is detected and surfaced with a specific field-level error message before any state change is applied — 100% of validation errors must be non-silent.
- **SC-004**: Removing a symphony from config causes no disruption to in-flight performer sessions on other symphonies; active sessions on the removed symphony are cancelled immediately as part of the remove action, completing within the same request/response cycle.
- **SC-005**: Per-symphony persona overrides are applied correctly — a performer dispatched to Symphony A receives Symphony A's persona, not the global default, when an override is configured.
- **SC-006**: An existing single-symphony `config.yaml` (no `symphonies:` key) continues to work without modification after the upgrade — zero breaking changes for current users.
- **SC-007**: The Symphonies dashboard page loads in under 2 seconds with up to 10 configured symphonies. Operators configuring more than 10 symphonies see a clear warning before saving.
- **SC-008**: Every coordinare log line and prometheus metric scoped to a symphony includes a `symphony` label/field, enabling an operator to fully isolate activity, error rates, and dispatch counts for any individual symphony using standard log filtering or a metrics query tool.

---

## Assumptions

- **Backwards compatibility**: Any existing `config.yaml` without a `symphonies:` key will be treated as a single implicit symphony using the entire global config. No migration is required.
- **Auth per symphony**: Each symphony may have its own GitHub authentication credentials (token or app), allowing cross-org orchestration.
- **Performer roles are global**: The performer role configuration (`performers.*`) in the global defaults applies across all symphonies unless overridden per symphony. An operator may define a symphony-specific `performers` block to assign different backends or models to specific roles for that project.
- **File-based config only**: Coordinare's config is written to and read from a single `config.yaml` file on disk. No database or API-only config store is introduced by this feature.
- **Single dashboard instance**: The dashboard runs on one port and aggregates all symphony state. There is no per-symphony dashboard.
- **envsubst scope**: Environment variable substitution uses `os.path.expandvars` (or equivalent) applied at load time. Variables unset in the environment expand to empty strings unless the config validation detects the result as invalid.

---

## Dependencies

- **008-config-management**: The existing config discovery and `ProjectConfiguration` pydantic model is the base that this feature extends.
- **010-web-dashboard / 049-dashboard-redesign**: The existing dashboard infrastructure (FastAPI + SSE + vanilla JS) is the foundation for the new Symphonies management page.
- **019-performer-lifecycle**: Performer lifecycle management must be extended to track which symphony a performer is currently serving.
- **054-async-multi-card-orchestration**: The multi-card orchestration loop must be extended to fan out across multiple symphonies per cycle.
- **016-force-poll**: The force-poll mechanism should be extended to support per-symphony force-poll from the dashboard.
