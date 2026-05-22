# Feature Specification: Hermes Performer Backend

**Feature Branch**: `068-hermes-backend`
**Created**: 2026-05-21
**Status**: Draft
**Input**: User description: Add Hermes as a new performer backend inside coordinare's existing performer container, without changing coordinare's orchestration model. Coordinare still communicates only through the existing performer job protocol, issues, PRs, and PR comments. Hermes must not use messaging gateways, direct user communication, cron, or global personal memory.

## Clarifications

### Session 2026-05-21

- Q: What bounds a hung Hermes run so FR-010's "no indefinite loop" guarantee is testable? → A: Reuse the existing session-level `AGENT_TIMEOUT` (the same wall-clock cap that already applies to every other backend); on expiry the adapter forces a terminal `error` and tears the Hermes process/session down.
- Q: Which roles must Hermes support in MVP? → A: All roles coordinare currently dispatches — full parity with peer backends from day one, no role gating.
- Q: How does `relay_feedback` reach a running Hermes session? → A: Queue feedback and incorporate it into the next Hermes invocation (one-shot model, matching the existing claude_code / junie pattern in this repo). No persistent chat session is required.
- Q: When is the job-scoped Hermes profile directory cleaned up? → A: Unconditionally on any terminal outcome — success, error, stop, or timeout. Failure-debugging context goes to structured logs, not to retained temp directories.
- Q: Where should `score.persona_instructions` land when a backend has a native persona/system-prompt slot (e.g. Hermes' `SOUL.md`, Codex' `developerInstructions`, Claude Code's `--append-system-prompt`)? → A: Route it to the canonical slot for backends with a job-isolated location; drop the duplicate `## Role Instructions` section from the task prompt body. Backends without a job-isolated slot (Junie, opencode, opencode_compat, cursor) keep the existing prompt-body wiring — we do NOT write `AGENTS.md` / `.cursorrules` into the workspace because those paths would land in commits.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator selects Hermes for a performer (Priority: P1)

An operator configures a performer to use the Hermes backend by setting `backend: hermes` in coordinare's performer configuration. Coordinare dispatches cards to that performer using the existing job protocol, and the performer runs Hermes against the work without any other change to coordinare.

**Why this priority**: This is the entire feature surface area. Without it, Hermes cannot be selected as a backend and no value is delivered.

**Independent Test**: Stand up the performer with `backend: hermes` configured for a single role (e.g., `implementer`). Dispatch one card from coordinare and confirm the performer accepts the job, runs Hermes, and returns a terminal status through the existing protocol.

**Acceptance Scenarios**:

1. **Given** a performer configured with `backend: hermes`, **When** coordinare sends a job for any supported role, **Then** the performer launches Hermes with the role's persona and card context and reports `working` until completion.
2. **Given** an unknown backend name in performer config, **When** the performer starts, **Then** it rejects the configuration with a clear error referring to the supported backend list including `hermes`.
3. **Given** a Hermes run that completes successfully on an implementation card, **When** the performer finalizes the job, **Then** the existing performer wrapper opens a PR and reports `pr_opened` to coordinare exactly as it does for other coding backends.

---

### User Story 2 - Hermes only speaks through GitHub (Priority: P1)

While Hermes is running a job, the only human-visible output the operator sees about that job appears in the coordinare-managed GitHub issue or PR. Hermes never messages the operator directly, never schedules background tasks of its own, and never reads or writes the operator's personal Hermes profile.

**Why this priority**: This is a hard safety/architectural requirement. If Hermes can talk to the user out-of-band, the orchestration model breaks and the operator's personal Hermes state can be polluted by automated runs.

**Independent Test**: Run an end-to-end card with Hermes and inspect: (a) the operator's chat/messaging surfaces stayed silent, (b) the operator's `~/.hermes` profile was not read or modified, (c) all human-facing output appears only as issue/PR/PR-comment activity authored by the performer's GitHub identity.

**Acceptance Scenarios**:

1. **Given** a Hermes-backed performer running any role, **When** the job executes, **Then** Hermes is launched with messaging-gateway, direct-messaging, cron, and clarify capabilities disabled.
2. **Given** a Hermes-backed performer, **When** the job executes, **Then** Hermes uses an isolated profile directory (job-scoped for MVP) and does not read from or write to the operator's default Hermes profile location.
3. **Given** a completed Hermes run, **When** the operator reviews what happened, **Then** every human-readable artifact attributable to the run is locatable on the coordinare-managed issue or PR.

---

### User Story 3 - Hermes behaves like the other coding backends (Priority: P2)

Hermes participates in the same performer lifecycle as the existing coding backends: it receives a structured prompt built from the same card context, exposes `working` / `done` / `error` status to coordinare, accepts relay feedback while running, and can be stopped cleanly. Coordinare does not gain any Hermes-specific code paths.

**Why this priority**: Lifecycle parity is what lets coordinare remain unchanged. Without it, coordinare would need a new orchestration path, which the feature explicitly forbids.

**Independent Test**: Compare a Hermes run against an existing backend (e.g., opencode) for the same role and card. Status transitions, relay-feedback handling, and stop semantics should be observably equivalent from coordinare's side.

**Acceptance Scenarios**:

1. **Given** a Hermes-backed job in progress, **When** coordinare sends relay feedback (e.g., a reviewer comment), **Then** the feedback is queued by the adapter and incorporated into the next Hermes invocation for that job, the same one-shot pattern the existing `claude_code` and `junie` backends use — no persistent mid-run chat session is required.
2. **Given** a Hermes-backed job, **When** coordinare requests stop, **Then** the Hermes process/session terminates and the performer reports a terminal status without leaking processes or temp directories.
3. **Given** a Hermes-backed assessor or architect role, **When** the role completes, **Then** the performer wrapper posts the role-appropriate review/comment and returns the matching status (e.g., `approved`, `changes_requested`, `qa_passed`) without any Hermes-specific status codes leaking to coordinare.

---

### Edge Cases

- Hermes produces empty or malformed output: the performer reports backend `error` and the job ends rather than looping indefinitely.
- Hermes process crashes or exits non-zero mid-run: surfaced as `error` to coordinare with the failure captured in performer logs.
- Hermes credentials/model env are missing or invalid: the performer fails fast at job start with a clear configuration error, not silently mid-run.
- Two Hermes jobs run concurrently on the same performer: each must use a distinct isolated profile directory so neither sees the other's state.
- Stop is requested before Hermes emits any output: the session is terminated and the job finalizes as cancelled/error without hanging.
- Operator's personal Hermes profile exists on the host: it is never touched, even when default Hermes config conventions would point there.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The performer MUST accept `hermes` as a backend identifier in performer configuration and instantiate a Hermes-specific adapter for jobs targeting that performer.
- **FR-001a**: The Hermes adapter MUST support every role coordinare currently dispatches (including but not limited to assessor, architect, implementer, reviewer, QA, security, and closer) at parity with existing coding backends. No role gating, feature flags, or "implementer-only" subsetting in MVP.
- **FR-002**: The Hermes adapter MUST conform to the same backend adapter contract used by existing coding backends, exposing start, status, event drain, relay-feedback, and stop behaviors with semantics indistinguishable from peers. `relay_feedback` follows the one-shot pattern (queue and incorporate on next invocation) used by `claude_code` and `junie`; mid-run streaming into a live Hermes session is not required.
- **FR-003**: The Hermes adapter MUST build its prompt from the same card context the other coding backends consume, including role/persona instructions, card title and description, acceptance criteria, clarifications, relay feedback, architecture-plan reference, and role-specific output requirements.
- **FR-004**: The Hermes adapter MUST run Hermes non-interactively (programmatic invocation), never through a chat/gateway UI.
- **FR-005**: Each Hermes invocation MUST use an explicit, isolated profile directory scoped to the job (MVP) and MUST NOT read from or write to the operator's default personal Hermes profile location.
- **FR-006**: The Hermes adapter MUST launch Hermes with messaging/gateway, direct user-communication, cron, clarify, and shared user-profile-memory capabilities disabled, leaving only a coding-oriented toolset (terminal/process, file read/write/patch/search, optional browser, todo, and optional symphony-scoped memory/skills).
- **FR-007**: Coordinare's orchestration code MUST require no changes to support Hermes; the existing performer HTTP protocol, role/persona passthrough, and status vocabulary MUST be sufficient.
- **FR-008**: The performer wrapper MUST continue to own all GitHub-visible side effects (commits, PR creation, reviews, QA/security comments) and MUST continue to return the existing status set (e.g., `pr_opened`, `approved`, `changes_requested`, `qa_passed`) for Hermes-backed runs.
- **FR-009**: The Hermes adapter MUST translate Hermes-internal lifecycle states into the shared `working` / `done` / `error` status vocabulary; no Hermes-specific status MAY leak out of the adapter.
- **FR-010**: Empty, malformed, or otherwise unparseable Hermes output MUST resolve to a terminal `error` status; the performer MUST NOT loop, retry indefinitely, or report false success.
- **FR-010a**: The Hermes adapter MUST be bounded by the existing session-level `AGENT_TIMEOUT` already applied to other backends. When that timeout is exceeded the adapter MUST force a terminal `error`, terminate the Hermes process/session, and clean up the job-scoped profile directory — no new Hermes-specific timeout mechanism is introduced.
- **FR-011**: A stop request MUST terminate the underlying Hermes process or session and clean up the job-scoped profile directory and any other job-local temp state. Cleanup MUST also run unconditionally on every other terminal outcome (success, `error`, and timeout per FR-010a) so no job-scoped profile directory survives the job that owned it. Failure-debugging context belongs in structured logs, not in retained temp directories.
- **FR-012**: Configuration documentation MUST include a `backend: hermes` example and document the env vars Hermes requires (profile location, provider/base URL, API key, model).
- **FR-013**: The Hermes adapter MUST be exercised by tests covering: backend factory registration, prompt and env propagation, persona passthrough, status transitions, stop semantics, disabled communication toolsets, and malformed-output handling.
- **FR-014**: A documented smoke-test path MUST exist for running one low-risk card end-to-end (assessor or architect first, then implementer on a tiny change) and verifying that no direct user messages were sent and all human-visible output is on GitHub.
- **FR-015**: The Hermes adapter MUST write `score.persona_instructions` to `$HERMES_HOME/SOUL.md` during `start()` so that hermes-agent loads it as the agent's identity rather than auto-generating a starter file with a default persona. When `score.persona_instructions` is empty or unset, the adapter MUST NOT write `SOUL.md` (letting hermes-agent's starter behaviour apply). The duplicate `## Role Instructions` section MUST be removed from the prompt body when `SOUL.md` is written, so persona content is not delivered twice.
- **FR-016**: The Claude Code adapter MUST pass `score.persona_instructions` via the documented `--append-system-prompt` CLI flag (the canonical, job-isolated identity slot for that backend), and MUST remove the duplicate `## Role Instructions` section from the prompt body. When `score.persona_instructions` is empty or unset, the flag MUST NOT be passed.
- **FR-017**: The Codex adapter MUST continue to set `developerInstructions` on the thread (already implemented at `codex.py:240`) and MUST remove the duplicate `## Role Instructions` section from the prompt body so persona is not delivered twice.
- **FR-018**: Backends without a job-isolated native persona slot (Junie, opencode, opencode_compat, cursor) MUST retain the existing prompt-body `## Role Instructions` wiring. This spec does NOT introduce repo-resident persona files (`AGENTS.md`, `.cursorrules`, etc.) because those paths sit inside the workspace and would risk landing in commits.

### Key Entities

- **Hermes Backend Adapter**: The per-job component that owns the lifecycle of a Hermes invocation on behalf of the performer. Receives the role and card context, launches Hermes in an isolated profile, surfaces status and events, accepts relay feedback, and tears down on stop.
- **Hermes Profile Directory**: A filesystem location holding all Hermes per-instance state for one job. Created at job start, removed at job end, never the operator's default location.
- **Hermes Capability Set**: The explicit allow/deny list of Hermes tools the adapter enables for a job — coding tools allowed, communication and scheduling tools forbidden.

## Assumptions

- Hermes is (or will be) packaged into the existing performer container image; this feature does not specify how Hermes is installed, only how it is invoked once present.
- The initial integration path is whichever of Hermes' supported programmatic entrypoints (direct `AIAgent` instantiation, non-interactive CLI subprocess, or internal programmatic API) is most stable in the version baked into the performer image. The adapter is structured so the chosen path can change without affecting coordinare.
- Symphony-scoped Hermes profile directories are a future enhancement; MVP uses a job-scoped temp directory only.
- Distributed model serving for Hermes is out of scope; the model endpoint Hermes talks to is whatever the operator configures via env.
- The exact env-var names for Hermes configuration follow Hermes' own current conventions; this feature lists the categories (profile path, provider, base URL, API key, model) rather than fixing names that may drift with Hermes upstream.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can switch a performer from any existing coding backend to Hermes by changing one configuration line, with no other configuration or code change required.
- **SC-002**: For a representative implementation card, a Hermes-backed performer produces an opened PR through the existing performer wrapper with the same set of status transitions an operator would observe from any other coding backend.
- **SC-003**: Across the smoke-test run, zero direct user messages are emitted by Hermes outside the GitHub issue/PR surface, and the operator's personal Hermes profile location is untouched (verifiable by file-system inspection before and after).
- **SC-004**: Concurrent Hermes jobs on the same performer never read or write each other's profile state (verifiable by isolated profile paths and a concurrency test).
- **SC-005**: Malformed or empty Hermes output resolves to a terminal `error` within one job lifecycle — never an indefinite loop — measured by the coordinare seeing a terminal status for 100% of failure-injection test cases.
- **SC-006**: Coordinare's source tree gains zero new orchestration branches for Hermes (verifiable by diff: changes confined to performer adapter files, config example, and tests).
- **SC-007**: HermesBackend cold start (from `start()` invocation to the adapter reporting `working`) adds no more than 200ms over the `junie` baseline measured on the same host during the FR-014 smoke run.
- **SC-008**: For Hermes, Claude Code, and Codex runs, the literal string of `score.persona_instructions` appears in exactly one location per invocation — `SOUL.md` / `--append-system-prompt` argv / `developerInstructions` respectively — and does NOT appear anywhere in the task prompt body (verifiable by grepping the persisted prompt artifact and the canonical slot during the FR-014 smoke run).
