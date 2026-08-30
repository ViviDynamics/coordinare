# Feature Specification: Config Assistant

**Feature Branch**: `155-config-assistant`
**Created**: 2026-08-30
**Status**: Draft
**Issue**: #202 (called "148: Config Assistant" in the launch roadmap; `148-` was taken by an
existing branch, so this is spec 155 — the `specs/` sequence, not the roadmap numbering)

## Context

Configuring coordinare means editing YAML: a GitHub project and token, a model endpoint, a set of
personas, a symphony. An operator who has never seen the schema has to work out which of those
they need and what the fields mean before anything runs at all. The dashboard already renders
every section as typed, described settings (spec 081), and spec 145 added presets — but reading
a form is still not the same as being told what to do next.

This adds a chat panel that helps an operator get to a working configuration, by **proposing**
changes they apply.

## The one thing this must not become

The assistant never writes configuration. Not "is careful about writing", not "asks first" —
it has no way to write. Applying is a human clicking Apply in the existing UI, through the
existing atomic write and its content-hash guard.

That is not caution for its own sake. Coordinare's config holds the GitHub token's env-var name,
the model endpoints, and which personas may act; a wrong write is an outage or a leak, and the
dashboard is unauthenticated by design at launch (spec 144). An assistant that could write would
be a way to change coordinare's behaviour by talking to it.

## Clarifications

- Q: Agentic tool-calling loop, as the issue describes? → A: **No.** Spec 124 tried exactly that
  and found tool-calling unreliable across every self-hosted model available
  (`qwen3.6:35b`, `glm-4.7-flash`, `gpt-oss:120b`), and cloud models are not permitted for this
  role. It shipped a single-shot structured contract instead, which worked. Building an agentic
  loop here would repeat a failure this project has already paid for. **Structured, yes; agentic
  loop, no** — one constrained JSON response per turn.
- Q: Where does the model come from? → A: the conducting backend coordinare already configures
  and already uses for its own reasoning. BYO-model and "no cloud requirement" then need no new
  machinery, because that backend is already whatever the operator pointed it at.
- Q: How is "never writes" guaranteed? → A: structurally. The assistant module contains no write
  path, and a test asserts it — not a rule the prompt asks the model to follow.
- Q: What stops secrets reaching the model? → A: for secrets held in *configuration*, the same
  masking the config UI already uses — replaced before any prompt is built, with a test asserting
  a real secret value cannot appear in prompt text. For text the operator *types*, nothing does,
  and the panel says so: filtering arbitrary prose for secrets is unreliable, and a broader claim
  would invite the pasted token it cannot prevent.
- Q: On by default? → A: no. Off unless enabled and a backend exists, and the dashboard is fully
  functional without it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The assistant proposes, and cannot apply (Priority: P1)

An operator describes what they want. The assistant answers, and where a config change would
help, proposes one: which section, what changes, and why. The operator sees the diff and decides.

**Why this priority**: this is the feature, and it is the whole safety property. It is also
testable with no UI at all, which is where the guarantees belong.

**Independent Test**: drive the assistant with a scripted backend, and assert it produced a valid
proposal — and that nothing in the module can write config.

**Acceptance Scenarios**:

1. **Given** a question about a config field, **When** the operator asks, **Then** they get an
   answer grounded in the actual descriptors rather than invented.
2. **Given** a request to change something, **When** the assistant responds, **Then** it returns
   a proposal naming the section and the new values, and the config on disk is unchanged.
3. **Given** a proposal, **When** it is checked, **Then** the operator is told whether it would
   validate, before they apply it.
4. **Given** a model that returns malformed or unparseable output, **When** the turn is handled,
   **Then** the operator sees a plain failure rather than a broken proposal.
5. **Given** a proposal naming a section or field that does not exist, **When** it is handled,
   **Then** it is rejected rather than passed on to the write path.

---

### User Story 2 - Applying is the operator's click (Priority: P1)

The operator reads a proposed diff and applies it. It goes through the same validation, atomic
write and concurrency guard as any other config edit.

**Why this priority**: also P1, because a proposal nobody can act on is not a feature. Separate
from US1 because the guarantee in US1 is that this path is the *only* one.

**Independent Test**: apply a proposal and confirm the change landed through the existing write
service; then apply a stale one and confirm it is refused.

**Acceptance Scenarios**:

1. **Given** an applied proposal, **When** it is written, **Then** it goes through the existing
   validate-then-atomic-write path.
2. **Given** a proposal generated before someone else edited the file, **When** the operator
   applies it, **Then** it is refused rather than overwriting that edit.
3. **Given** an applied change, **When** the operator looks at the config UI, **Then** it shows
   the new values.

---

### User Story 3 - Stored secrets never reach the model (Priority: P1)

An operator whose config contains a token, key or password can use the assistant without those
values being sent anywhere. What they type is a different matter, and the panel says so.

**Why this priority**: P1 and non-negotiable. The assistant sends config to a model endpoint that
may be a third party; a leak here is the config's most sensitive content going somewhere the
operator did not choose.

**Independent Test**: put a real-looking secret in config, run a turn, and assert the value
appears in no prompt the backend received.

**Acceptance Scenarios**:

1. **Given** a secret-bearing field, **When** context is built, **Then** the value is masked and
   only its name and env-var indirection are visible.
2. **Given** a proposal, **When** it touches a secret field, **Then** it can set the env-var name
   but never a literal secret value — and not a literal riding alongside a reference either.
3. **Given** the panel, **When** an operator is about to type, **Then** it states that what they
   type goes to the model endpoint as written.

---

### User Story 4 - A first-run operator is walked to a working config (Priority: P2)

Someone who has just installed coordinare and configured nothing is offered the golden path:
board, model endpoint, the minimum to run.

**Why this priority**: the strongest reason the feature exists, but it depends on US1-US3 being
right, and an operator with a broken half-configured install is worse off than one reading YAML.

**Independent Test**: from an empty config, follow the assistant's proposals and reach a
configuration that validates.

**Acceptance Scenarios**:

1. **Given** an unconfigured install, **When** the operator opens the assistant, **Then** it
   offers the first step rather than an empty prompt.
2. **Given** the proposals are applied in order, **When** the last is applied, **Then** the
   configuration validates.

---

### Edge Cases

- **No backend configured.** The feature is off, and the dashboard behaves exactly as it does
  today.
- **The backend is slow or down.** The operator is told; the dashboard keeps working.
- **A model that argues.** Nothing it can say changes what it is able to do, because it has no
  write path.
- **A proposal against a section that changed since.** The hash guard refuses it.
- **A very large config.** Context has to be bounded; the assistant should say what it could not
  see rather than silently truncating.
- **Chat history.** Session-scoped; nothing about a config conversation is persisted.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The assistant MUST be able to read configuration structure and current values.
- **FR-002**: The assistant MUST NOT have any means of writing configuration.
- **FR-003**: A proposal MUST name the section it targets and the values it sets.
- **FR-004**: A proposal MUST be validated and the result shown before it can be applied.
- **FR-005**: A proposal naming an unknown section or field MUST be rejected.
- **FR-006**: Applying MUST use the existing validate → atomic write → content-hash path.
- **FR-007**: A proposal built against a stale version of the file MUST be refused.
- **FR-008**: Secret values held in configuration MUST be masked everywhere the model can see
  them. This does not extend to text the operator types: a chat cannot filter arbitrary prose
  for secrets reliably, and pretending otherwise would invite the pasted token it cannot stop.
  The panel MUST say plainly that what is typed goes to the model endpoint.
- **FR-009**: A proposal MUST NOT be able to set a literal secret value; env-var names only.
- **FR-010**: The model MUST be the operator's already-configured conducting backend, so a
  self-hosted endpoint works and no cloud service is required.
- **FR-011**: Each turn MUST be a single constrained structured response, not a multi-step tool
  loop.
- **FR-012**: Malformed model output MUST surface as a plain failure, never a partial proposal.
- **FR-013**: The feature MUST be off unless explicitly enabled and a backend exists.
- **FR-014**: The dashboard MUST be fully functional with the assistant disabled.
- **FR-015**: Conversation state MUST be session-scoped and unpersisted.
- **FR-016**: The assistant MUST NOT be able to take any non-config action.
- **FR-017**: Context sent to the model MUST be bounded, and any omission stated rather than
  silent.
- **FR-018**: The assistant MUST be reachable only under the same trust boundary as the rest of
  the config UI.

### Key Entities

- **Turn**: one operator message and one structured response.
- **Proposal**: a section, the values to set, and the reasoning. Inert until applied.
- **Validation result**: whether a proposal would produce a valid configuration.
- **Masked view**: the configuration as the model may see it — structure and non-secret values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The assistant cannot write configuration, demonstrated structurally rather than by
  observing that it did not.
- **SC-002**: A real secret value placed in config appears in nothing sent to the model. Text
  the operator types is out of that guarantee and is stated as such in the panel.
- **SC-003**: Every applied change goes through the existing write path, including the
  concurrency guard; a stale proposal is refused.
- **SC-004**: The assistant works against a self-hosted endpoint, with no cloud service involved.
- **SC-005**: With the feature disabled, the dashboard behaves exactly as before.
- **SC-006**: Malformed model output never produces a proposal an operator could apply.
- **SC-007**: From an empty configuration, following the assistant's proposals reaches one that
  validates.
- **SC-008**: No existing dashboard test is modified.

## Out of Scope

- The assistant taking any action other than proposing config: no card operations, vetoes,
  dispatches, or merges.
- Authentication for the dashboard (spec 143 / #197).
- Persisting conversations.
- Changing the config schema, the write service, or the config UI's existing behaviour.
- Fine-tuning or evaluating which model is best at this; any configured backend is supported and
  quality is the operator's choice.
