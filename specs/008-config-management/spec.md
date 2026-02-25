# Feature Specification: Configuration Management

**Feature Branch**: `008-config-management`
**Created**: 2026-02-25
**Status**: Draft
**Input**: User description: "Config management improvements for the coordinare daemon — validation, environment variable overrides for CI/CD, multi-location config discovery, and deprecation detection when config schema changes."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Config Validation Without Starting the Daemon (Priority: P1)

As an operator, I want to validate my `config.yaml` against the current schema without starting the coordinare daemon, so that I can catch configuration errors before deployment without any side effects.

**Why this priority**: Config errors today are only discovered when the daemon starts — often in production. A standalone validate command closes the feedback loop and is the single highest-value improvement for operators. Every other config improvement depends on having a reliable validation layer.

**Independent Test**: Run `coordinare config validate` against a valid config file and verify it exits zero with a success summary. Run it against a config file with a missing required field (e.g., `github_token` absent) and verify it exits non-zero and prints the exact field name and a human-readable fix hint.

**Acceptance Scenarios**:

1. **Given** a valid `config.yaml`, **When** the operator runs `coordinare config validate`, **Then** the command prints a one-line success summary and exits with code 0.
2. **Given** a `config.yaml` with a missing required field, **When** the operator runs `coordinare config validate`, **Then** the command prints the field path, a description of what is missing, and exits with code 1.
3. **Given** a `config.yaml` with a type mismatch (e.g., a string where an integer is expected), **When** the operator runs `coordinare config validate`, **Then** all type errors are reported in a single pass (not one at a time) and the command exits with code 1.
4. **Given** no config file found at any searched path, **When** the operator runs `coordinare config validate`, **Then** the command prints which paths were searched and exits with code 1.

---

### User Story 2 - Environment Variable Overrides for CI/CD (Priority: P2)

As an operator running coordinare in CI/CD or containerised environments, I want all config fields to be overridable via environment variables, so that I can inject secrets and environment-specific values without committing sensitive data or modifying `config.yaml` at deploy time.

**Why this priority**: CI/CD and container deployments cannot rely on a file-based secrets pattern. Without env var overrides, operators must either bake secrets into images or use fragile file-injection scripts. This is the second most critical capability for production readiness.

**Independent Test**: Set `COORDINARE_GITHUB_TOKEN=test123` in the environment with no `github_token` field in `config.yaml`. Run `coordinare config validate`. Verify the field is resolved and no missing-field error appears. Verify a field set in both config.yaml and an env var uses the env var value (env vars win).

**Acceptance Scenarios**:

1. **Given** a config field is absent from `config.yaml` but present as an env var (`COORDINARE_<FIELD_NAME>`), **When** coordinare loads config, **Then** the env var value is used and no missing-field error is raised.
2. **Given** the same field is set in both `config.yaml` and an env var, **When** coordinare loads config, **Then** the env var value takes precedence over the file value.
3. **Given** an env var is set for a nested scalar config field using double-underscore notation (e.g., `COORDINARE_GITHUB__POLL_INTERVAL_SECONDS`), **When** coordinare loads config, **Then** the nested field is correctly populated.
4. **Given** no config file exists at any search path but all required fields are supplied via env vars, **When** coordinare starts, **Then** it starts successfully without requiring a config file.

---

### User Story 3 - Multi-Location Config Discovery (Priority: P3)

As an operator, I want coordinare to automatically search standard filesystem locations for `config.yaml` so that I don't need to pass `--config` on every invocation, and the daemon works out-of-the-box in both development and production layouts.

**Why this priority**: Requiring `--config /path/to/config.yaml` on every invocation is friction. Standard tools (git, ssh, docker) search well-known paths by convention. This is quality-of-life rather than a blocking capability.

**Independent Test**: Place a valid `config.yaml` in `~/.coordinare/config.yaml` with no `--config` flag. Run `coordinare config validate`. Verify it finds and validates the file without any flag.

**Acceptance Scenarios**:

1. **Given** a `config.yaml` exists in the working directory (`./config.yaml`), **When** coordinare starts without `--config`, **Then** it loads the working-directory config.
2. **Given** no `config.yaml` in the working directory but one exists at `~/.coordinare/config.yaml`, **When** coordinare starts without `--config`, **Then** it loads the home-directory config.
3. **Given** the env var `COORDINARE_CONFIG_PATH` is set to a custom path, **When** coordinare starts, **Then** it loads from that path regardless of the default search order.
4. **Given** a `--config` flag is passed explicitly, **When** coordinare starts, **Then** it loads only from that path and ignores all other search locations.
5. **Given** the `--config` flag points to a non-existent file, **When** coordinare starts, **Then** it exits with a clear error naming the missing file path.

---

### User Story 4 - Deprecation Detection and Migration Guidance (Priority: P4)

As an operator, I want coordinare to detect deprecated or removed config fields in my `config.yaml` and print precise migration instructions, so that I know exactly what to change when the config schema evolves between releases.

**Why this priority**: Spec 006 introduced a breaking config change (removed flat `smtp_*` and `slack_*` fields, replaced with a `notifications:` block). Without deprecation detection, operators only discover the breakage when the daemon fails to start. This is essential for smooth upgrades.

**Independent Test**: Place a `config.yaml` containing the removed flat field `slack_webhook_url`. Run `coordinare config validate`. Verify it reports a deprecation warning naming the removed field, the spec version that removed it, and the replacement path (`notifications.channels[].webhook_url`).

**Acceptance Scenarios**:

1. **Given** a `config.yaml` containing a field that was removed in a prior spec (e.g., `slack_webhook_url`), **When** the operator runs `coordinare config validate`, **Then** the output names the removed field, identifies the spec that removed it, and provides the replacement config structure.
2. **Given** a config file with both deprecated fields and missing required fields, **When** the operator validates, **Then** deprecation warnings and validation errors are reported separately and clearly distinguished.
3. **Given** a fully up-to-date `config.yaml` with no deprecated fields, **When** the operator validates, **Then** no deprecation warnings appear in the output.
4. **Given** `coordinare config validate --strict`, **When** any deprecated field is present, **Then** the command exits with code 1 (deprecations treated as errors, useful for CI pipelines).

---

### Edge Cases

- What happens when a required field is set to an empty string via env var? The system treats an empty string as absent — a missing-field error is raised.
- What happens when `COORDINARE_CONFIG_PATH` points to a directory rather than a file? The command exits with a clear error: "config path must point to a file, not a directory."
- What happens when two config files at different search locations have conflicting values? Only the highest-priority file is loaded (first match wins in discovery order); no merging of multiple files occurs.
- What happens when an env var value cannot be coerced to the expected type (e.g., `COORDINARE_POLL_INTERVAL_SECONDS=abc`)? The validation layer reports a type error for the env var source, identifying both the env var name and the expected type.
- What happens when `config.yaml` contains a field name not present in the schema (e.g., a typo like `githubb_token`)? The system treats it as an error — the daemon refuses to start and `coordinare config validate` exits 1 with a message identifying the unknown field and suggesting the closest valid field name if detectable.
- What happens when `coordinare config validate` is run during daemon startup failure? The validate command is a separate CLI subcommand — it never starts the daemon. The daemon startup path uses the same validation logic but emits structured log errors.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a `coordinare config validate` CLI subcommand that validates the resolved configuration (file + env vars) against the current schema and reports all errors in a single pass without starting the daemon; validation covers: missing required fields, type mismatches, unknown fields, and deprecated fields.
- **FR-002**: The `coordinare config validate` command MUST exit with code 0 on success and code 1 on any validation error (including unknown fields, missing required fields, and type mismatches); the daemon MUST also refuse to start when any of these errors are present, to prevent unexpected runtime behavior from misconfiguration.
- **FR-003**: All scalar and nested-scalar configuration fields MUST be overridable by an environment variable following the naming convention `COORDINARE_<FIELD_NAME>` for top-level fields and `COORDINARE_<PARENT>__<FIELD_NAME>` for nested scalar fields; env var values MUST take precedence over file values. List-type fields (e.g., `notifications.channels`) cannot be overridden via env vars and MUST be sourced from the config file.
- **FR-004**: When all required configuration fields are supplied via environment variables alone (no config file present), coordinare MUST start successfully.
- **FR-005**: Coordinare MUST search for `config.yaml` in the following priority order: (1) path given by `--config` flag, (2) `COORDINARE_CONFIG_PATH` env var, (3) `./config.yaml` (working directory), (4) `~/.coordinare/config.yaml` (user home); the first match is used and no further paths are searched.
- **FR-006**: When no config file is found at any search location and no required fields are provided via env vars, coordinare MUST exit with a clear error listing all searched paths.
- **FR-007**: The system MUST maintain a deprecation registry mapping removed or renamed config fields to: the spec version that removed them, and the replacement field path or migration instruction.
- **FR-008**: `coordinare config validate` MUST report deprecated fields as warnings by default and as errors when `--strict` is passed; the output for each deprecated field MUST include the old field name, the removing spec version, and the replacement structure.
- **FR-009**: Validation error output MUST identify the field path (e.g., `notifications.channels[0].webhook_url`), the error type (missing, wrong type, deprecated, unknown field), and a plain-English fix hint for each error.
- **FR-010**: The daemon's startup path MUST reuse the same config validation logic as `coordinare config validate`; a config that passes `coordinare config validate` MUST also allow the daemon to start without config-related errors.
- **FR-011**: The system MUST emit a structured log entry (`structlog`) at startup reporting: which config file was loaded (or "no file — env vars only"), how many fields were resolved from env vars, and whether any deprecated fields were detected.
- **FR-012**: `coordinare config validate --strict` MUST exit with code 1 if any deprecated fields are present, enabling CI pipelines to enforce migration compliance.

### Key Entities

- **ConfigSource**: Represents one input to the resolved configuration. Attributes: source type (file, env_var, default), path or variable name, precedence rank.
- **ResolvedConfig**: The final merged configuration after applying all sources in precedence order. Contains: field values, per-field source provenance (which ConfigSource provided each value).
- **ConfigValidationResult**: The outcome of a validation pass. Contains: list of `ConfigFieldError` (field path, error type, fix hint), list of `DeprecationWarning` (old field, spec version, replacement), overall pass/fail status.
- **DeprecationRegistry**: A static mapping of removed/renamed fields to their removal context. Populated at build time from spec history; not user-configurable.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `coordinare config validate` completes in under 500ms on any valid or invalid config file; operators receive immediate feedback without daemon startup overhead.
- **SC-002**: A config file containing all deprecated fields from spec 006 produces a deprecation report that names every removed field and its replacement — zero undocumented removals.
- **SC-003**: A valid config supplied entirely via environment variables (no config file) allows coordinare to start and pass all existing test suites without modification.
- **SC-004**: All validation errors across a config file are reported in a single run of `coordinare config validate` — operators never need to run the command multiple times to discover all errors.
- **SC-005**: `coordinare config validate --strict` returns exit code 1 for any config containing deprecated fields, enabling CI pipelines to enforce migration compliance without custom scripting.
- **SC-006**: The startup log entry produced when coordinare loads config is machine-parseable (structured JSON) and contains: config file path (or "none"), count of env-var-resolved fields, and a boolean `deprecated_fields_detected`.

## Assumptions

- **A-001**: All configuration fields are defined as Pydantic model fields on `ProjectConfiguration` and its nested models; the deprecation registry is maintained as a separate data structure alongside the config model.
- **A-002**: Environment variable naming for nested fields uses double-underscore (`__`) as the separator, matching the pydantic-settings convention already in use; no custom separator logic is introduced. Env var overrides apply to scalar and nested-scalar fields only; list-of-object fields (e.g., `notifications.channels`) require the YAML file and cannot be injected via env vars. All CI/CD-sensitive fields (tokens, credentials, scalar settings) are scalar, so this scoping is sufficient for production use cases.
- **A-003**: Config file merging (combining multiple files) is explicitly out of scope; only a single file is loaded at the highest-priority matching location.
- **A-004**: External secret stores (AWS Secrets Manager, HashiCorp Vault, etc.) are out of scope for this spec; env var injection covers the CI/CD use case adequately.
- **A-005**: The `coordinare config validate` command is a subcommand of the existing `coordinare` CLI entry point (`__main__.py`) — no new top-level binary is introduced.
- **A-006**: Deprecation is report-only (not auto-migrate); the system tells operators what to change but does not modify their config file. Auto-migration may be introduced in a later spec.
- **A-007**: The deprecation registry for fields removed in spec 006 (`slack_webhook_url`, `slack_channel`, `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `notification_email`) is populated as part of this spec's implementation.

## Clarifications

### Session 2026-02-25

- Q: Should unknown fields in `config.yaml` (fields not present in the schema) be treated as warnings, errors, or silently ignored? → A: Errors — an unknown field likely indicates a typo whose intended value is silently not applied; the daemon must refuse to start and `coordinare config validate` must exit 1 to prevent unexpected runtime behavior from misconfiguration.
- Q: Should env var overrides support list-type fields (e.g., indexed notation `COORDINARE_NOTIFICATIONS__CHANNELS__0__WEBHOOK_URL`) or scalar/nested-scalar fields only? → A: Scalar and nested-scalar fields only — pydantic-settings list-of-objects env var support requires substantial custom parsing code, the UX is fragile (numeric indices break if YAML list order changes), and all CI/CD-critical fields (tokens, credentials, scalar settings) are scalars; list-type fields such as `notifications.channels` must be sourced from the config file.
