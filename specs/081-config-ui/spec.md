# Feature Specification: Live Config Editing in the Dashboard UI

**Feature Branch**: `081-config-ui`
**Created**: 2026-06-06
**Status**: Draft
**Input**: User description: "Live Config Editing in the Dashboard UI — make the coordinare's configuration surface viewable and live-editable from the dashboard, presented so an operator can understand what each setting does without reading source."

## Clarifications

### Session 2026-06-06

- Q: Is the spec-078 self-hosted routing config editable (full CRUD) from the dashboard in this feature, or view-only with editing deferred? (FR-012) → A: Full CRUD now — create/read/update/delete routing entries, target descriptors, normalizers, and strategy/reroute, validated against the 078 pydantic models with atomic write-back, including the coordinare→performer write plumbing.
- Q: Should the spec-080 model catalogs (endpoints / model_endpoints / modes) be in scope as first-class CRUD sections? → A: Yes — full CRUD for `endpoints`, `model_endpoints`, and `modes`, each validated against its pydantic model.
- Q: How should `${VAR}` env-placeholder-backed values be shown and edited? → A: Display the raw `${VAR}` literal (never the expanded value, and never masked — the placeholder names an env var, not a secret); edits operate on the literal text. Only a non-placeholder secret value stored directly in the file is masked. The placeholder indirection is always preserved.
- Q: When config.yaml or the routing-config file is edited out-of-band while the dashboard has it open, how should a save behave? → A: Detect & warn (optimistic concurrency) — capture a file hash/mtime at load, and on save reject with a "file changed externally — reload to merge" warning rather than clobbering the unseen change.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - See and understand the whole configuration (Priority: P1)

An operator opens the dashboard and views the coordinare's complete configuration in one place — every setting that governs how the coordinare runs — each shown with a human-readable label, a plain-language description of what it does, its current value, its type, its valid range or allowed values, and its default. Settings are grouped into logical sections (e.g. scheduling/tuning, personas, per-symphony overrides, backend/model config, self-hosted routing). Editable settings are visually distinguished from read-only/derived ones. The operator can understand what each setting does without opening source code.

**Why this priority**: Visibility is the foundation. Today large parts of config (most notably the spec-078 self-hosted routing table) are invisible in the UI — an operator cannot even confirm what the system is running on. Read-only comprehension delivers standalone value and is a prerequisite for safe editing.

**Independent Test**: Load the dashboard config view against a known config.yaml (and a known performer routing config) and confirm every documented setting appears with label, description, current value, type, allowed range/enum, and default, correctly grouped, with editable vs read-only clearly marked — without any edit being made.

**Acceptance Scenarios**:

1. **Given** a running coordinare with a populated config.yaml, **When** the operator opens the config view, **Then** all configuration sections render with each setting's label, help text, current value, type, valid range/enum, and default.
2. **Given** a self-hosted routing table is configured (spec 078), **When** the operator opens the config view, **Then** the routing table — its entries, target descriptors, normalizers, and strategy/reroute settings — is visible (at minimum read-only).
3. **Given** a setting is derived or read-only, **When** the operator views it, **Then** it is visually marked as not editable and the UI offers no edit affordance for it.
4. **Given** a setting holds a secret/token, **When** the operator views it, **Then** the secret value is masked and never shown in full.

---

### User Story 2 - Edit a configuration value safely and apply it live (Priority: P2)

An operator changes an editable setting in the UI, sees their input validated inline (against the same rules the coordinare enforces) before anything is saved, and on save the new value is validated server-side, written back atomically to its source, and applied without restarting the coordinare wherever it is safe to hot-reload. Settings that cannot take effect without a restart are clearly flagged as restart-required so the operator knows the edit is staged, not yet live.

**Why this priority**: Editing is the core promise of the feature, but it depends on US1's surface and metadata. It must be safe (no corrupting config, no half-applied edits, no leaked secrets) before it is convenient.

**Independent Test**: Edit a known hot-reloadable setting (e.g. poll interval) to a valid value, save, and confirm: the value persists to its backing store, the running coordinare reflects the new value without a restart, and a subsequent reload of the view shows the new value.

**Acceptance Scenarios**:

1. **Given** an editable hot-reloadable setting, **When** the operator enters a valid value and saves, **Then** the value is persisted and applied to the running coordinare without a restart, and the UI confirms success.
2. **Given** an editable setting, **When** the operator enters an invalid value (out of range, wrong type, fails a model constraint), **Then** the UI rejects it with a clear inline error and nothing is persisted.
3. **Given** an editable setting that requires a restart to take effect, **When** the operator saves a valid change, **Then** the value is persisted and the UI clearly flags that a restart is required for it to take effect.
4. **Given** a save operation, **When** the value is written to its backing file, **Then** the write is atomic — a failure mid-write never leaves the config file truncated or corrupt.
5. **Given** a configuration save, **When** the action is logged or reported, **Then** no secret/token value appears in logs or responses.
6. **Given** a valid edit that passes server-side validation and is written to `config.yaml`, **When** the subsequent `POST /api/config/reload` fails, **Then** the daemon continues running on the **previous in-memory config** (no partial application), and the UI surfaces an actionable error — "Saved to disk, but live reload failed; the running config is unchanged. Restart to apply." — with `applied: staged_restart` and **no** stack trace.

---

### User Story 3 - Manage the spec-078 self-hosted routing config from the dashboard (Priority: P3)

An operator views and edits the performer-scoped self-hosted routing configuration (routing table entries, target descriptors, normalizers, strategy/reroute) from the central dashboard, with the same understandable presentation and validation as the rest of config — closing the gap that motivated this feature.

**Why this priority**: This is the concrete gap that prompted the feature, but it is the hardest tier because the routing config is performer-scoped (a YAML file referenced by `SELFHOSTED_ROUTING_CONFIG`, loaded at performer job start), not part of the coordinare's own config.yaml. It requires deciding/implementing the coordinare→performer config plumbing, so it sits behind the general capability.

**Independent Test**: With a routing config present, view it in the dashboard, edit one target descriptor's strategy and its normalizer list to a valid combination, save, and confirm the change is validated against the spec-078 pydantic models, written back, and picked up by the next performer job.

**Acceptance Scenarios**:

1. **Given** a self-hosted routing config, **When** the operator views it in the dashboard, **Then** entries, target descriptors, normalizers, and strategy/reroute are shown with labels, help, allowed values, and defaults.
2. **Given** an edit to a routing entry, **When** the operator sets `strategy=normalize` with an empty normalizer list (or `strategy=reroute` with a non-empty normalizer list), **Then** the UI rejects it with a clear inline error per the spec-078 model constraints, before persisting.
3. **Given** a valid routing-config edit, **When** the operator saves, **Then** it is validated against the spec-078 pydantic models and written back atomically to the routing-config file.
4. **Given** a routing-config change, **When** a new performer job starts, **Then** the job loads the updated routing config.

---

### Edge Cases

- **External edit / drift**: When config.yaml (or the routing-config file) is edited on disk out-of-band while the dashboard has it open, the save MUST NOT silently clobber the unseen change. Resolved via optimistic concurrency: a file hash/mtime is captured at load, and on save a mismatch is rejected with a "file changed externally — reload to merge" warning (see FR-016).
- **Concurrent dashboard editors**: Two operators editing the same setting simultaneously — how is the conflict resolved?
- **Partial/invalid existing config**: The on-disk config already contains a value that no longer validates (e.g. after a schema change). How is it displayed, and can the operator still edit other settings?
- **`${VAR}` placeholders**: config.yaml uses `${VAR}` env-placeholder expansion at load time. Resolved: the dashboard displays the raw `${VAR}` literal (never the expanded value), edits operate on the literal text so the placeholder indirection is preserved, and secret-flagged placeholders are masked (see FR-017).
- **Reload failure**: A hot-reload (`POST /api/config/reload`) fails after a successful write — is the prior in-memory config preserved, and is the operator told the file changed but the live value did not?
- **Routing-config absent**: `SELFHOSTED_ROUTING_CONFIG` is unset/empty (the disabled/no-op default) — the UI must present routing config as "disabled/not configured" and offer to create one, rather than erroring.
- **Restart-required setting**: A restart-required value is saved but the coordinare is never restarted — the UI must keep signalling the staged-vs-live divergence.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST present the full editable configuration surface — global tuning, personas, per-symphony overrides, backend/model config, the spec-080 model catalogs (`endpoints`, `model_endpoints`, `modes`), and the spec-078 self-hosted routing table (entries, target descriptors, normalizers, strategy/reroute) — not only today's `_global_cfg_editable` subset.
- **FR-002**: Each configuration setting MUST be displayed with a human-readable label, a plain-language description, its current value, its type, its valid range or enum of allowed values, and its default.
- **FR-003**: The UI MUST visually distinguish editable settings from read-only/derived settings, and MUST NOT offer an edit affordance for read-only/derived settings.
- **FR-004**: Settings MUST be organized into logical, labeled sections so an operator can locate and understand related settings together.
- **FR-005**: The system MUST validate operator input against the existing pydantic config models (including spec-078 routing models) and reject invalid input with a clear, specific inline error before persisting anything.
- **FR-006**: Validation MUST be enforced server-side (client-side validation, if any, is an aid only and never the authority).
- **FR-007**: On a valid save, the system MUST write the changed value back to its backing store atomically — a failure during write MUST NOT leave the config file truncated or corrupt.
- **FR-008**: Where a changed setting can take effect without a restart, the system MUST apply it to the running coordinare via the existing hot-reload path without requiring a restart.
- **FR-009**: Settings that require a restart to take effect MUST be flagged as restart-required, and after saving such a setting the UI MUST clearly indicate the change is persisted but not yet live.
- **FR-010**: The system MUST never display secret/token values in full, and MUST never write secret/token values to logs or include them in API responses.
- **FR-011**: The dashboard MUST be able to view the spec-078 self-hosted routing configuration even though it is a performer-scoped surface (a YAML file referenced by `SELFHOSTED_ROUTING_CONFIG`, loaded at performer job start), establishing coordinare→performer config visibility.
- **FR-012**: The system MUST allow full CRUD on the spec-078 self-hosted routing configuration from the dashboard — create, read, update, and delete routing entries, target descriptors, normalizers, and strategy/reroute settings — with every edit validated against the spec-078 pydantic models and written back atomically to the routing-config file. This includes the coordinare→performer write plumbing required to persist a performer-scoped surface from the central dashboard.
- **FR-013**: When the self-hosted routing configuration is absent/disabled (the `SELFHOSTED_ROUTING_CONFIG` empty default), the dashboard MUST present it as not-configured rather than erroring.
- **FR-014**: The system MUST preserve the existing config CRUD/reload/persona endpoints' behavior (it extends, not replaces, the current `/api/config/*` and `/api/personas/*` surface).
- **FR-015**: A failed hot-reload after a successful write MUST leave the previously-loaded in-memory configuration intact and MUST inform the operator that the file changed but the live value did not.
- **FR-016**: On save, the system MUST use optimistic concurrency to guard against out-of-band edits: it MUST capture a content hash (or mtime) of the backing file at load and, if the on-disk file has changed since, MUST reject the save with a clear "file changed externally — reload to merge" warning rather than overwriting the unseen change. This applies to both config.yaml and the performer routing-config file.
- **FR-017**: Values backed by a `${VAR}` env placeholder MUST be displayed as the raw `${VAR}` literal (never the expanded/resolved value, and never masked — a `${VAR}` literal names an environment variable, not a secret, so it is shown verbatim even on a secret-flagged field); edits MUST operate on the literal placeholder text so the placeholder indirection is preserved. Only a non-placeholder secret value stored directly in the file MUST be masked. The system MUST NOT silently replace a `${VAR}` placeholder with its expanded literal value on save.
- **FR-018**: The dashboard MUST provide full CRUD on the spec-080 model catalogs — `endpoints` (name, kind, base_url, auth_env), `model_endpoints` (name, endpoint, model), and `modes` (name, strategy, tool) — as their own labeled sections, with create/update/delete and validation against the corresponding pydantic models. Referential integrity MUST be enforced (a `model_endpoints` entry MUST reference an existing `endpoint`; a `mode` MUST reference an existing tool/`model_endpoints` entry; a referenced catalog entry MUST NOT be deletable while a performer still references it).

### Key Entities *(include if feature involves data)*

- **Config Setting (descriptor)**: A single editable or read-only configuration field surfaced to the UI. Attributes: key/path, label, description/help, type, current value, default, valid range/enum, editable flag, restart-required flag, secret flag, owning section, backing store (coordinare config.yaml vs performer routing-config).
- **Config Section**: A named, logical grouping of settings (e.g. scheduling, personas, per-symphony overrides, backends/models, model catalogs, self-hosted routing).
- **Model Catalog (spec 080)**: The three reusable catalogs declared once and referenced per performer by `mode` — `endpoints` (name, kind, base_url, auth_env), `model_endpoints` (name, endpoint→endpoints, model), and `modes` (name, strategy, tool→model_endpoints) — surfaced through the dashboard with full CRUD and referential-integrity enforcement, backed by config.yaml.
- **Routing Config (spec 078)**: The performer-scoped routing table — `RoutingTable` of `RoutingEntry` (backend, model, target) with `TargetDescriptor` (base_url, wire_format, strategy, normalizers, reroute_upstream) — surfaced through the dashboard, backed by the file referenced by `SELFHOSTED_ROUTING_CONFIG`.
- **Edit/Save action**: An operator change to one or more settings, validated and persisted, carrying outcome state (applied-live / persisted-restart-required / rejected-with-errors).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of configuration settings that are documented as editable are visible and editable in the dashboard (no editable setting is reachable only by hand-editing files).
- **SC-002**: The spec-078 self-hosted routing configuration is visible in the dashboard (it has zero visibility today).
- **SC-003**: An operator unfamiliar with the source can correctly state what a given setting does, its valid values, and its default using only the dashboard — verified for a representative sample of settings across all sections.
- **SC-004**: 100% of invalid edits are rejected with a clear inline error and result in no change to the backing config file.
- **SC-005**: A valid edit to a hot-reloadable setting takes effect in the running coordinare without a restart, confirmed within one reload cycle.
- **SC-006**: No secret/token value ever appears in the UI in full, in API responses, or in logs (verified by inspection across all config sections).
- **SC-007**: No save operation produces a truncated or unparseable config file, including under an induced mid-write failure.
- **SC-008**: The spec-080 model catalogs (`endpoints`, `model_endpoints`, `modes`) and the spec-078 routing table both support full create/update/delete from the dashboard, with referential-integrity violations and model-constraint violations rejected with a clear inline error and no change to the backing file.
- **SC-009**: A save attempted against a backing file that changed on disk after load is rejected with a "file changed externally" warning and makes no modification (verified by inducing an out-of-band edit between load and save).
- **SC-010** (performance budget): The full config view (all sections, including catalogs and routing table) returns its descriptor payload in under 300 ms p95 server-side for a representative config, and renders to interactive in under 1.5 s p95 in the browser; a single save round-trip (validate + atomic write, excluding hot-reload propagation) completes in under 500 ms p95. These budgets are enforced by an automated benchmark in CI.
