# Feature Specification: Onboarding config hardening

**Feature Branch**: `132-onboarding-config-hardening`
**Created**: 2026-07-31
**Status**: Draft
**Input**: GitHub issue #180 — "132: onboarding config hardening — fix first-run config.example.yaml walls"

## User Scenarios & Testing *(mandatory)*

A new operator adopts Coordinare by cloning the repo, copying `config.example.yaml` to
`config.yaml`, supplying their required identity/credentials (GitHub token, org, project
number), and starting the daemon. Today that path hits several avoidable walls. Each user
story below removes one wall and is independently shippable.

### User Story 1 - First run launches the performer with no script edits (Priority: P1)

A first-time operator copies the example config and starts Coordinare. The configured
performer entry point must work on their fresh machine without editing any script under
`bin/` and without a performer binary pre-installed at a system path.

**Why this priority**: This is the highest-impact wall — it *hard-blocks* the first run.
The shipped default (`agent_executable: "/usr/local/bin/performer"`) does not exist on a
fresh install, and the repo's `bin/performer` script contains a hardcoded personal home
path that fails everywhere else. Nothing else in onboarding matters if the performer can't
launch.

**Independent Test**: On a clean clone with no performer installed system-wide, set
`agent_executable` to the shipped default, start Coordinare, and confirm the performer
process launches successfully — with zero edits to any `bin/` script.

**Acceptance Scenarios**:

1. **Given** a fresh clone and an unedited example config, **When** the operator starts Coordinare, **Then** the performer launches using the shipped default entry point without editing any `bin/` script.
2. **Given** the repository as checked in, **When** a reviewer inspects the performer entry-point script(s), **Then** no script contains a developer-specific absolute home path.
3. **Given** the example config, **When** the operator reads both the active `agent_executable` line and its commented reference block, **Then** both point at the same working, portable default.

---

### User Story 2 - Copying the example config validates cleanly without secrets (Priority: P2)

An operator who has not set up Slack or SMTP copies the example config unedited and starts
Coordinare. Configuration validation must pass without requiring any notification secrets.

**Why this priority**: This blocks the first run for anyone who hasn't opted into
notifications, but there is a known workaround (empty the `notifications` block), so it
ranks below the performer-launch blocker. The example currently ships two *active*
channels (`slack-ops` needing a webhook, `email-team` needing SMTP creds); a fresh user
who hasn't configured those gets a validation failure on a feature they never opted into.

**Independent Test**: Copy `config.example.yaml` unedited (beyond required identity
fields), with no `COORDINARE_SLACK_WEBHOOK_URL` / SMTP credentials set, and confirm config
validation passes.

**Acceptance Scenarios**:

1. **Given** no Slack webhook and no SMTP credentials in the environment, **When** the operator validates the copied example config, **Then** validation succeeds.
2. **Given** the shipped example, **When** an operator wants to enable Slack or email, **Then** a complete, commented reference block for each is present in the file to copy from.

---

### User Story 3 - A paused symphony is obvious at startup (Priority: P3)

An operator sets `enabled: false` on a symphony to pause it. At startup the operator can
see an unambiguous log line stating that the symphony is paused because of that setting,
rather than inferring it from silent inactivity.

**Why this priority**: This is not a bug — pausing via `enabled: false` is documented and
correct. It is purely an observability/clarity improvement, so it is lowest priority.

**Independent Test**: Configure a symphony with `enabled: false`, start Coordinare, and
confirm a clear per-symphony "paused" line naming the symphony appears at startup.

**Acceptance Scenarios**:

1. **Given** a symphony configured with `enabled: false`, **When** Coordinare starts, **Then** it emits an unambiguous log line naming the symphony and stating it is paused because `enabled: false`.
2. **Given** a paused symphony over a long run, **When** many poll cycles elapse, **Then** the pause is communicated clearly at startup rather than as repeated noise on every cycle.

### Edge Cases

- A relative `agent_executable` (e.g. `bin/run-performer`) must resolve correctly regardless of the directory Coordinare is started from, or the documented default must be expressed so it always resolves. *(Resolution behavior to be confirmed in research — see plan.)*
- Paused-symphony logging must behave in both multi-symphony and legacy single-symphony configurations.
- The paused-symphony line must not regress into per-cycle log spam (today a warning fires every cycle).
- "Unedited" excludes the required identity/credential fields an operator must always supply (GitHub token, org, project number); those are expected edits, not onboarding walls.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The example configuration's default performer entry point (`agent_executable`) MUST reference a wrapper that runs successfully on a fresh clone with no machine-specific edits and no pre-installed system binary.
- **FR-002**: The repository MUST NOT ship a performer entry-point script that contains a hardcoded, developer-specific absolute path. `bin/performer` MUST be retained as a **minimal compatibility wrapper that delegates to `bin/run-performer`** — it MUST NOT duplicate launch logic and MUST NOT be deleted in this PR (decision: approver-confirmed).
- **FR-003**: Every reference to the performer entry point in the example config (the active line and the commented example block) MUST be consistent with the corrected default.
- **FR-004**: Copying the example configuration unedited (beyond required identity/credential fields) MUST pass configuration validation with no Slack webhook and no SMTP credentials configured.
- **FR-005**: The example configuration MUST retain complete, commented reference blocks for the Slack and email notification channels so the setup path remains discoverable.
- **FR-006**: When a symphony is configured with `enabled: false`, the system MUST emit an unambiguous startup log line naming the symphony and stating that it is paused because of `enabled: false`.
- **FR-007**: The paused-symphony indication MUST be communicated clearly at startup and MUST NOT produce repeated per-cycle log noise for the same paused symphony.
- **FR-008**: Any onboarding/quickstart documentation that references the old performer entry-point path or the previous notification defaults MUST be updated to match the corrected example.
- **FR-009**: All changes MUST remain scoped to onboarding/example-config hardening; no personal-account support (spec 133) behavior may be introduced.

### Key Entities

Not applicable — this feature introduces no new data entities or persisted state. It
changes an example configuration file, a wrapper script, and a startup log line.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user who clones the repo, copies `config.example.yaml`, and sets only the required identity/credential fields can start Coordinare and have the performer launch with **zero** edits to any `bin/` script.
- **SC-002**: Copying `config.example.yaml` unedited (beyond required identity/credential fields) passes configuration validation with **no** Slack or SMTP secrets present.
- **SC-003**: With a symphony set to `enabled: false`, exactly **one** unambiguous per-symphony "paused" line is emitted at startup, and no per-cycle pause warning recurs for that symphony.
- **SC-004**: **Zero** checked-in files contain a developer-specific absolute home path for the performer entry point.
- **SC-005**: A reviewer can enable Slack or email later using only the commented reference blocks already present in the example file (no external lookup required).
- **SC-006**: A symphony disabled via `enabled: false` has its paused status emitted **exactly once during startup** and produces **zero** recurring per-cycle log entries for that paused state over the lifetime of the run.

## Assumptions

- The canonical, portable performer wrapper is `bin/run-performer` (resolves the repo root relative to the script, sources `.env`, runs via `uv run --project agent/performer`). The corrected `agent_executable` default points at it.
- **Decision (approver-confirmed): keep `bin/performer` as a minimal compatibility wrapper that delegates to `bin/run-performer`.** It must not duplicate launch logic and must not be deleted in this PR. Rationale: preserves the legacy entry point for anyone/anything referencing it, while making it portable by delegating to the single canonical wrapper (no divergent launch paths to maintain). The pre-existing working-tree edit to `bin/performer` (which invokes `.venv/bin/python -m performer` directly — i.e. duplicated launch logic) will be **replaced** by the thin delegation to `bin/run-performer`.
- "Notifications off by default" is expressed by shipping empty `channels`/`routing` with the live examples preserved as comments; no notification *validation code* changes.
- Required identity/credential fields (GitHub token, org, project number) are always operator-supplied and are out of scope as "walls."
