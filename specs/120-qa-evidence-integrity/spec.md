# Feature Specification: QA Evidence Integrity, Toolchain Availability & Visual-Evidence Capture

**Feature Branch**: `120-qa-evidence-integrity`
**Created**: 2026-06-25
**Status**: Draft
**Input**: User description: "QA evidence integrity + env-cache toolchain on the QA performer's PATH + dual-path visual-evidence capture. One combined spec hardening the website QA stage end-to-end."

## Context

The QA stage is the orchestrator's last automated gate before a card moves toward human approval and merge. A live QA run on the website project (card #177, 2026-06-25) exposed three compounding failures in a single result:

1. The QA performer reported **`Result: PASSED`** while simultaneously reporting **`Criteria passed: 0` of 5**, an explicit list of remaining failures, and **no visual artifacts captured**. The orchestrator advanced the card on that "pass."
2. The QA performer could not run the application because the project's language runtime (Ruby) was **not on its execution PATH**, even though the project's cached development environment contains that runtime and activates it correctly for other stages.
3. Because the application never started, **no screenshots / visual evidence** were captured for a visual/UI change.

Investigation during specification confirmed the cached environment and the performer image are both correct and current — the runtime *is* in the cache and *does* activate for the bootstrap stage. The QA stage is not receiving (or not activating) that environment the way other stages do. Separately, the orchestrator trusts the QA performer's self-reported "pass" without confirming any criterion actually passed or any required evidence exists.

This feature hardens the QA stage end-to-end so that (a) an unsubstantiated "pass" can never advance a card, (b) the QA performer always has the project's runtime and services available exactly as the bootstrap stage does, and (c) visual changes are proven with at least one captured screenshot via whichever browser tooling is easiest, with a backstop.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The gate rejects an unsubstantiated "pass" (Priority: P1)

A QA performer returns a terminal verdict of "passed" but the result shows zero acceptance criteria actually passed and/or no execution or visual evidence. The orchestrator must refuse to treat this as a success: the card must not advance. Instead, if the result also carries an environment-failure signal, the card is held as environment-blocked and the operator is notified; otherwise the verdict is treated as a failure and the work bounces back for another attempt.

**Why this priority**: Until this is closed, QA is not a real gate — broken or unverified work can pass straight through to human review, defeating the entire purpose of the stage. This is a correctness/safety defect and is the highest priority.

**Independent Test**: Feed the QA monitor a synthetic terminal result with `passed=true`, `criteria_passed=0`, empty evidence, and (a) with an environment-error signal → card is held as environment-blocked with a notification; (b) without an environment-error signal → card is treated as failed and bounced. Neither case advances the card.

**Acceptance Scenarios**:

1. **Given** a QA result reporting passed with zero criteria passed and no evidence **and** an accompanying environment-failure signal, **When** the orchestrator evaluates the verdict, **Then** the card is held in the environment-blocked state, the operator is notified, and the card does not advance.
2. **Given** a QA result reporting passed with zero criteria passed and no evidence **and** no environment-failure signal, **When** the orchestrator evaluates the verdict, **Then** the verdict is recorded as failed, the card bounces for another attempt, and the card does not advance.
3. **Given** a QA result reporting passed with at least one criterion passed and the required evidence present, **When** the orchestrator evaluates the verdict, **Then** the card advances normally (no regression of legitimate passes).
4. **Given** any downgraded verdict, **When** the decision is recorded, **Then** the recorded reason names the cause (e.g. "zero criteria passed", "missing visual evidence", "environment error") using only names/counts/reasons and no secret values.

---

### User Story 2 - The QA performer has the project's runtime and services (Priority: P1)

A QA performer dispatched for a project with a cached development environment must run its commands in a shell where the project's language runtime, tools, and started services are available — exactly as they are for the environment-bootstrap stage. When that environment genuinely cannot be made available, the QA run must surface an environment error (which feeds the User Story 1 routing) rather than proceeding to a misleading verdict.

**Why this priority**: Without the runtime, the QA performer cannot start the application, run the test suite, or capture screenshots — so it can verify nothing. This is the root cause of the empty, runtime-less QA run and a prerequisite for User Story 3. It is co-equal P1 with the gate.

**Independent Test**: Dispatch a QA performer for a project whose cache contains a language runtime; inside the QA execution environment, the runtime resolves on PATH and the application boots. Inspect the run's observability record: it states whether the cached environment was attached and whether activation succeeded.

**Acceptance Scenarios**:

1. **Given** a project with a cached development environment containing a language runtime, **When** a QA performer is dispatched, **Then** that runtime resolves on the QA performer's command PATH and the application can start — identically to the bootstrap stage.
2. **Given** a successful QA dispatch, **When** the run begins, **Then** an observability record states whether the cached environment was attached and whether activation succeeded, using names/reasons only.
3. **Given** the cached environment cannot be attached or activation fails, **When** the QA run proceeds, **Then** the run surfaces an environment error and does not report a substantive pass — routing into the User Story 1 environment-blocked path.
4. **Given** a project with services defined in its cached environment, **When** a QA performer runs, **Then** those services are available to the QA performer the same way they are at bootstrap.

---

### User Story 3 - Visual changes are proven with a captured screenshot (Priority: P2)

For any change that affects the user interface, the QA stage must produce at least one screenshot proving the application rendered. The QA performer is free to drive whatever browser tooling is already available in its environment to capture this evidence in-turn; a separate post-QA capture step acts as a backstop. A "pass" verdict on a visual change is not accepted unless at least one visual-evidence artifact exists.

**Why this priority**: Visual confirmation is the most valuable QA output for UI work and is what was missing. It depends on User Story 2 (the app must boot) and is enforced by User Story 1 (no evidence → not a pass), so it is P2 — high value, but built on the two P1 stories.

**Independent Test**: Run the QA stage against a UI change with the runtime available; confirm at least one screenshot artifact is attached to the result. Then simulate the in-turn capture failing and confirm the backstop step still produces an artifact when the app is running, and that a visual change with zero artifacts cannot yield a "pass".

**Acceptance Scenarios**:

1. **Given** a UI change and an available runtime, **When** the QA performer runs, **Then** it captures at least one screenshot as visual evidence using whichever browser tooling it chooses, with no single tool mandated.
2. **Given** the QA performer did not capture a screenshot but the application is running, **When** the post-QA backstop step runs, **Then** the backstop captures at least one screenshot.
3. **Given** the application is not running, **When** the post-QA backstop step runs, **Then** it does not silently report success with zero artifacts; it records that capture was not possible.
4. **Given** a change flagged as requiring visual validation, **When** the QA result contains zero visual-evidence artifacts, **Then** the verdict cannot be "passed" (it is failed or environment-blocked per User Story 1).

---

### Edge Cases

- **Legitimate zero-criteria runs**: If a QA scope genuinely has no acceptance criteria to check (non-UI, no testable behavior), the gate must distinguish "nothing to verify" from "claimed to verify but produced nothing" so it does not falsely block trivially-valid work. (Default: a "passed" with criteria-checked > 0 but criteria-passed = 0 is always unsubstantiated; criteria-checked = 0 is handled per the existing scope rules.)
- **Partial pass**: Some criteria pass, some fail. The verdict must reflect failure (not advance) — a partial pass is not a pass.
- **Evidence present but environment error also present**: Environment-blocked routing takes precedence so the run is retried with a healthy environment rather than counted as a real failure (avoids burning the bounce budget on infrastructure faults).
- **Backstop capture races the ephemeral container**: If the QA performer's container has already exited when the backstop runs, the backstop records that the app was not reachable rather than producing a misleading empty success.
- **Repeated environment-blocked holds**: Honors the existing environment-blocked hold and notification behavior; does not loop indefinitely.
- **Non-UI changes**: Visual-evidence preconditions apply only to changes flagged as requiring visual validation; non-visual changes are not blocked for lacking screenshots.

## Requirements *(mandatory)*

### Functional Requirements

**Verdict integrity (User Story 1)**

- **FR-001**: The orchestrator MUST NOT advance a card on a QA terminal "passed" verdict when zero acceptance criteria passed.
- **FR-002**: The orchestrator MUST NOT advance a card on a QA terminal "passed" verdict that is missing evidence required for its scope (execution evidence for testable criteria; at least one visual artifact for changes requiring visual validation).
- **FR-003**: When a QA "passed" verdict is downgraded under FR-001/FR-002 **and** an environment-failure signal is present, the orchestrator MUST hold the card as environment-blocked and notify the operator (consistent with existing environment-blocked behavior).
- **FR-004**: When a QA "passed" verdict is downgraded under FR-001/FR-002 **and** no environment-failure signal is present, the orchestrator MUST treat the verdict as failed and route the card to its normal failed/bounce path.
- **FR-005**: A QA terminal success accompanied by a failed environment-health signal MUST NOT advance the card silently (carries forward the prior bootstrap/QA integrity principle).
- **FR-006**: Legitimate "passed" verdicts (≥1 criterion passed with required evidence present) MUST continue to advance the card with no added friction.
- **FR-007**: Every downgrade decision MUST be recorded with a reason that names the cause using only names, identifiers, counts, and reasons — never secret values.

**Toolchain availability (User Story 2)**

- **FR-008**: A QA performer dispatched for a project with a cached development environment MUST execute its commands in an environment where that project's runtime and tools resolve on PATH, equivalent to the environment-bootstrap stage.
- **FR-009**: Services defined by the project's cached environment MUST be available to the QA performer the same way they are at bootstrap.
- **FR-010**: Each QA dispatch MUST emit an observability record stating whether the cached environment was attached and whether its activation succeeded, using names/reasons only.
- **FR-011**: When the cached environment cannot be attached or its activation fails, the QA run MUST surface an environment error (feeding FR-003) and MUST NOT report a substantive "passed" verdict.

**Visual-evidence capture (User Story 3)**

- **FR-012**: For a change requiring visual validation, the QA stage MUST produce at least one screenshot proving the application rendered.
- **FR-013**: The QA performer MUST be permitted to capture that screenshot in-turn using whichever browser tooling is already available in its environment; no single tool may be mandated.
- **FR-014**: A post-QA backstop capture step MUST attempt to capture a screenshot when the QA performer did not, and MUST run only when there is proof the application is reachable.
- **FR-015**: The backstop step MUST NOT report success while capturing zero artifacts; if capture is not possible it MUST record that fact.
- **FR-016**: For a change requiring visual validation, a result containing zero visual-evidence artifacts MUST NOT yield a "passed" verdict (enforced via FR-002).

**Cross-cutting invariants**

- **FR-017**: No new external dependency may be introduced; browser tooling and the backstop capture step already exist in the environment.
- **FR-018**: Any new persisted state MUST be a backward-compatible addition to the existing single-host snapshot model (parallel to existing bootstrap/environment-blocked fields); no migration of existing state.
- **FR-019**: All new logs, records, and operator notifications MUST carry only names, identifiers, SHAs, kinds, counts, and reasons — never secret values.

### Key Entities

- **QA verdict**: The terminal outcome of a QA run — its pass/fail/environment-blocked classification, the count of criteria checked vs. passed, and the evidence it carries.
- **QA evidence**: The proof attached to a verdict — execution records (commands/tests run and their results) and visual artifacts (screenshots). Its presence or absence determines whether a "pass" is substantiated.
- **Environment-health signal**: An indicator on a QA result that the project's cached environment was unavailable or failed to activate; routes a downgraded verdict to environment-blocked rather than failed.
- **Cached development environment**: The per-project environment (runtime, tools, services) that must be available to the QA performer identically to the bootstrap stage.
- **Visual-validation requirement**: A per-change flag indicating the change affects the UI and therefore requires at least one screenshot as evidence.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of QA results reporting "passed" with zero criteria passed are prevented from advancing the card (no false passes reach human review).
- **SC-002**: 100% of QA dispatches for a project with a cached runtime result in that runtime being resolvable in the QA performer's execution environment (the application can start).
- **SC-003**: 100% of QA runs for changes requiring visual validation that end in "passed" carry at least one screenshot artifact.
- **SC-004**: A downgraded or environment-blocked QA verdict can be diagnosed entirely from its recorded reason and observability record, with no need to inspect the live container.
- **SC-005**: Legitimate QA passes (≥1 criterion passed with evidence) continue to advance with no measurable increase in false holds/bounces (no regression).
- **SC-006**: The recurrence of the observed failure (a "passed" verdict with zero criteria, no runtime, and no screenshots) drops to zero in subsequent QA runs of the affected project.

## Assumptions

- The project's cached development environment and its activation script are correct and current (confirmed during specification); this feature does not modify them.
- The performer image is current and already ships the activation profile and browser tooling (confirmed during specification); this feature does not rebuild it.
- The QA stage for the affected project uses the standard agent harness for that project; non-standard harnesses are out of scope.
- The existing environment-blocked hold/notify behavior and the existing bounce/retry budget are the correct destinations for downgraded verdicts and are reused, not redesigned.
- "Required evidence" for a non-visual scope means execution evidence for the criteria checked; for a visual scope it additionally means ≥1 screenshot.

## Out of Scope

- Changing the cached environment contents or its activation script (confirmed correct).
- Rebuilding the performer image (confirmed current).
- The coordinare-managed-services subsystem (kept dormant behind its existing toggle; the affected project is performer-owned).
- QA harnesses other than the one the affected project uses.
- The choice of model used for QA.
- The upload/CDN mechanics of screenshots beyond capturing them and attaching them as visual evidence.
