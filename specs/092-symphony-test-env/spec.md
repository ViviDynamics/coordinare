# Feature Specification: Symphony Test-Environment Injection

**Feature Branch**: `092-symphony-test-env`
**Created**: 2026-06-16
**Status**: Draft
**Input**: User description: "Symphony test-environment injection. Add a first-class, symphony-level test environment concept: an optional `test_env` config block on SymphonyConfig naming a dotenv-style file (repo_path XOR host_path), a coordinare `test_env_loader.load_test_env` service that parses dotenv-style with no shell exec, injection into the inference start-phase dry-run + QA runtime + code-running performers, a path-only `test_env_source` fallback field on ServicesManifest, and the spec-091 secret invariant (names/paths only, never values). Fixes the website symphony postgres Connection refused QA failure."

**Authoritative design**: `docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md` (approved). This spec restates that design in SpecKit form; on any discrepancy the design doc governs.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configured test-env unblocks stateful-service QA (Priority: P1)

A symphony owner whose project needs a stateful service (e.g. postgres) at QA time
declares a test-environment file at the symphony level. Coordinare reads the
`KEY=VALUE` pairs from that file and makes them present in every container that runs
project code: the service-inference start-phase validation dry-run, the env-cache QA
runtime where `services-start.sh` executes, and any other code-running performer (e.g.
the implementer). The postgres password the manifest references by name (e.g.
`POSTGRESQL_PASSWORD`) is now set, the start-phase gate passes, `initdb` runs, the
manifest is accepted, and the service boots at QA time.

**Why this priority**: This is the headline fix. Without it the `website` symphony's QA
fails with postgres "Connection refused" because the start-phase dry-run rejects the
manifest (the referenced password var is unset), leaving a stale `services-start.sh`
that never boots the database. It is the minimum viable slice that delivers the value.

**Independent Test**: Configure a symphony with a `test_env` block pointing at a file
defining the postgres password var; run the start-phase validation dry-run; confirm the
postgres gate passes (no `exit 75`) and the manifest is accepted. Confirmable end-to-end
by observing the `website` symphony's QA reaching the application instead of "Connection
refused".

**Acceptance Scenarios**:

1. **Given** a symphony with `test_env.repo_path` pointing at a committed file that
   defines the postgres password var, **When** the inference start-phase dry-run runs,
   **Then** the postgres gate sees the value, `initdb` succeeds, and the manifest is
   accepted.
2. **Given** the same symphony, **When** the env-cache QA runtime later runs
   `services-start.sh`, **Then** the test-env vars are present in the container
   environment and postgres boots.
3. **Given** the same symphony, **When** a code-running performer (e.g. the implementer)
   is dispatched, **Then** the test-env vars are present in its job environment.
4. **Given** a symphony whose test password is a genuine secret, **When** the owner sets
   `test_env.host_path` to a volume-mounted absolute path instead of `repo_path`,
   **Then** coordinare reads that host file through the same loader and injects the same
   way, without the secret ever being committed to the repo.

---

### User Story 2 - Agent-discovered fallback when no block is configured (Priority: P2)

A symphony owner has not declared a `test_env` block, but a conventional test-env file
exists in the repository. The inference agent identifies that file and records its
**path only** in the manifest (`test_env_source`). Coordinare parses that path through
the identical loader, injects the vars for the dry-run, and persists the path alongside
the env-cache so the QA-runtime and performer contexts reload the same file later.

**Why this priority**: Lets existing symphonies benefit without an explicit config
change, while keeping the explicit block as the deterministic, preferred path. Builds on
US1's loader and injection points.

**Independent Test**: Run inference on a repo that has a conventional test-env file and
no `test_env` config; confirm the manifest carries a path-only `test_env_source`, the
dry-run injects the discovered vars, and the persisted cache records the same path for
later reload.

**Acceptance Scenarios**:

1. **Given** no `test_env` config and a discoverable test-env file, **When** inference
   runs, **Then** the manifest's `test_env_source` holds the file's repo-relative path
   and no literal values.
2. **Given** a manifest with `test_env_source`, **When** the env-cache is built,
   **Then** the discovered path is persisted with the cache and reloaded for the
   QA-runtime and performer contexts.
3. **Given** neither a config block nor a discovered path provides the var the gate
   needs, **When** the start-phase dry-run runs, **Then** the existing `exit 75` gate
   fires with an actionable "requires env var X but it is unset" message — a genuine
   rejection, not a silent pass.

---

### User Story 3 - Secret invariant and error attribution preserved (Priority: P2)

An operator inspecting manifests, logs, and config can never see a literal test secret —
only env-var **names** and file **paths**. When a configured file is missing or
unreadable, the operator gets a clear, coordinare-side error that names the resolved path
and the field that pointed at it, rather than an opaque dry-run failure.

**Why this priority**: The spec-091 secret invariant is non-negotiable; violating it
would leak credentials into durable artifacts. Clear error attribution is what makes the
feature operable. Both ride alongside US1/US2 rather than being separately shippable.

**Independent Test**: Configure a `test_env` block pointing at a non-existent file and
confirm a distinct coordinare-side error naming the path and field; inspect captured logs
for a successful run and confirm only keys and source appear, never values.

**Acceptance Scenarios**:

1. **Given** a `test_env` block whose file is missing/unreadable, **When** coordinare
   attempts to load it, **Then** it raises a distinct, environment-attributed error
   naming the resolved path and the originating field — not a silent empty dict and not
   a buried `exit 75`.
2. **Given** any successful load, **When** structured log events are emitted, **Then**
   they contain only the var **names** (keys) and the **source** (repo_path / host_path
   / agent-discovered path), never any value.
3. **Given** a manifest produced by inference, **When** it is persisted, **Then** it
   contains only env-var names and file paths — never a literal secret value.

---

### Edge Cases

- **Both `repo_path` and `host_path` set** → hard config-validation error at load time
  (no silent precedence).
- **Neither set (empty `test_env` block)** → config-validation error; an empty block is
  meaningless and must be omitted instead.
- **`repo_path` escaping the clone** (`..` segments or an absolute path) → rejected by a
  containment check at config-load time, re-asserted at read time as defense in depth.
- **Value containing shell metacharacters** (e.g. `$(rm -rf /)`, backticks) → returned
  as a literal string; no command substitution or interpolation ever occurs.
- **A test-env key colliding with a coordinare operational secret** (e.g.
  `GITHUB_TOKEN`) → the operational secret wins, because operational secrets are
  injected **after** test-env vars.
- **Dotenv quirks** — surrounding quotes, a leading `export `, `#` comment lines, blank
  lines, `=` inside a value, CRLF line endings → handled per the dotenv parsing rules
  (one layer of matching quotes stripped, comments/blanks skipped, split on first `=`).
- **Reused cache across symphonies** → test-env vars are runtime-only environment, never
  baked into the cache image, so a reused cache stays correct.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support an optional, symphony-level `test_env`
  configuration block that names a single dotenv-style environment file.
- **FR-002**: The `test_env` block MUST accept exactly one of two mutually exclusive
  sources: `repo_path` (resolved inside the cloned symphony repository) or `host_path`
  (an absolute host path read directly by coordinare).
- **FR-003**: The system MUST reject, at config-load time, a `test_env` block that sets
  both sources or neither source (no silent precedence; no meaningless empty block).
- **FR-004**: The system MUST reject a `repo_path` that escapes the clone (relative
  `..` segments or an absolute path), enforced at config-load time and re-checked at
  file-read time.
- **FR-005**: The system MUST provide a single loader that parses the named file in
  dotenv style — splitting on the first `=`, skipping blank and `#`-comment lines,
  stripping one layer of matching surrounding quotes, and ignoring a leading `export ` —
  and returns a name→value mapping.
- **FR-006**: The loader MUST NOT execute any shell, perform command substitution, or
  interpolate variables; every value MUST be taken literally.
- **FR-007**: When a `test_env` block is configured but the file is missing or
  unreadable, the system MUST raise a clear, coordinare-side error that names the resolved
  path and the originating field — never silently return an empty mapping.
- **FR-008**: The system MUST inject the loaded variables into the environment of the
  service-inference start-phase validation dry-run.
- **FR-009**: The system MUST inject the loaded variables into the env-cache QA runtime
  environment in which `services-start.sh` executes.
- **FR-010**: The system MUST inject the loaded variables into the environment of other
  code-running performers (e.g. the implementer).
- **FR-011**: The system MUST inject coordinare-owned operational secrets (e.g.
  `GITHUB_TOKEN`, backend API keys) **after** the test-env variables, so a test-env file
  can never override an operational credential.
- **FR-012**: When no `test_env` block is configured, the inference agent MUST be able to
  record the path of a discovered test-env file in the manifest via a `test_env_source`
  field, carrying the **path only** and never any value.
- **FR-013**: The system MUST parse a `test_env_source` path through the same loader used
  for configured sources, and MUST persist the discovered path alongside the env-cache so
  the QA-runtime and performer contexts reload the same file.
- **FR-014**: When neither a configured block nor a discovered path supplies a variable
  that a service-start gate requires, the system MUST allow the existing start-phase gate
  to fail with an actionable "requires env var X but it is unset" message — a genuine
  rejection, not a silent pass.
- **FR-015**: The manifest and all logs MUST carry only env-var **names** and file
  **paths**, never literal secret values (the spec-091 secret invariant).
- **FR-016**: Loaded values MUST be treated as secret-like and routed through the
  existing redacted secrets channel; structured logs MUST emit only keys (var names) and
  the source (`repo_path` / `host_path` / agent-discovered path).
- **FR-017**: Injected test-env variables MUST be runtime environment only and MUST NOT
  be baked into the cache image, so that a reused cache remains correct across
  symphonies.

### Key Entities *(include if feature involves data)*

- **Test-env config block (`test_env`)**: A symphony-level configuration object naming
  one dotenv-style file. Attributes: `repo_path` (repo-relative path, mutually exclusive
  with `host_path`) and `host_path` (absolute host path). Exactly one is required.
- **Loaded test-env mapping**: An in-memory, secret-like name→value mapping produced by
  the loader from the named file. Never persisted as values; only its keys and source
  appear in logs.
- **`test_env_source` (manifest field)**: An optional, path-only string on the services
  manifest recording an agent-discovered test-env file when no config block is present.
  Carries a repo-relative path, never values.
- **Env-cache persisted discovery path**: The discovered `test_env_source` path stored
  alongside the env-cache so QA-runtime and performer contexts can reload the same file.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A symphony that requires postgres at QA time and declares a valid
  `test_env` block completes QA without a "Connection refused" failure (the `website`
  symphony's reproduction passes).
- **SC-002**: The start-phase validation dry-run accepts a manifest whose referenced
  password variable is supplied by the test-env file, and rejects it (with the existing
  actionable gate message) when no source supplies that variable — in 100% of cases.
- **SC-003**: No literal test-env value appears in any manifest, persisted artifact, or
  log line across all test scenarios; only names and paths appear (verified by inspecting
  captured logs and persisted state).
- **SC-004**: A misconfigured `test_env` block (both sources, neither source, or an
  escaping `repo_path`) is rejected at config-load time, before any job runs, in 100% of
  cases.
- **SC-005**: A configured-but-missing file produces a distinct coordinare-side error that
  names the path and field, with zero occurrences of a silent empty-mapping fallback.
- **SC-006**: The same loaded variables are present in all three code-running contexts
  (inference dry-run, QA runtime, other performers) for a given configured symphony.
- **SC-007**: The loader parses a typical test-env file (≤50 entries) in under 10 ms,
  adding no perceptible latency to config load or job dispatch (Constitution
  Principle IV planning budget; verified by a timed unit assertion).

## Assumptions

- The existing service-inference validator already accepts an `env=` parameter and merges
  it over `os.environ`; this feature threads the loaded mapping into that seam rather than
  introducing a new validation path.
- Code-running performers already receive a redacted `secrets` mapping in their job
  payload; test-env variables are merged into that existing channel.
- The env-cache persistence layer can store one additional path-only field for the
  discovered fallback source.
- A single test-env file per symphony is sufficient (see Out of Scope).

## Out of Scope *(YAGNI)*

- Multiple test-env files per symphony.
- Variable-name remapping or templating.
- Inter-variable interpolation within the file.
- Encrypted-at-rest files (the `host_path` volume-mount source already covers
  genuine-secret cases).

## Dependencies

- Builds on spec 091 (Stateful Service Hosting) and its secret invariant — env-var names
  in the manifest, never literal secrets.
- Relies on the existing start-phase validation dry-run, the env-cache QA runtime, and the
  performer payload `secrets` seam.
