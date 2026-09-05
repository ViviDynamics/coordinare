# Feature Specification: Reasoning Policy and Truncation Classification

**Feature Branch**: `163-reasoning-policy`
**Created**: 2026-09-04
**Status**: Draft
**Advances**: #244
**Extends**: Spec 098 (backend format-error retry), 119 (documenting malformed retry), 080 (model catalogs)
**Input**: A reasoning model that spends its whole output budget thinking returns an empty answer, and coordinare reports it as malformed output. Fix the misreport for every role, and where the model allows it, stop the situation arising at all.

## Overview

A reasoning-capable model can spend its entire output budget on thinking and return
**nothing usable**: the answer is empty, and the thinking is stranded in a separate field
the caller may never read. Coordinare currently reports that as *malformed output*.

Those two situations want opposite responses. Truncation should retry with more room, or
say plainly that the budget was too small. Malformed output should retry the format or
escalate. Conflating them sends an operator hunting for a better model when the fix is a
number in a config file. Spec 119 already hit exactly this misdiagnosis, where the real fix
turned out to be a retry rather than a different model.

This feature does two things:

- **Recognise it everywhere** (US1). The classification already exists and is correct; it
  is simply wired to one role. Unwire it.
- **Prevent it where the model cooperates** (US2). Some models accept a request-level
  instruction to stop producing separate reasoning. Where that genuinely helps, use it.
  Where it makes things worse, it must be impossible to enable by accident.

## Clarifications

### Session 2026-09-04

- Q: Should reasoning handling be fixed at the gateway rather than in coordinare? → A: **At
  the request, per model.** Modifying the gateway deployment is out of bounds, and a global
  request-level flag is unsafe (see the measured evidence below: the same flag helps two
  models and actively breaks a third). So the policy is a property **of a model**, carried
  in configuration, defaulting to no override.
- Q: Do both halves ship, or only the preventative one? → A: **Both.** The classification
  fix is the safety net for every model that does not cooperate, or does not opt in. The
  policy is the cause-level fix for those that do. Neither subsumes the other.
- Q: Which comes first? → A: **Classification (US1).** It is smaller, it is safe for every
  model, and it is what stops the misreport regardless of what any model does.

## The measured evidence this rests on

All observed through the gateway. These are findings, not assumptions.

**The failure is real on the model in use today.** On the currently served self-hosted
model, a tight output budget produces exactly the reported shape:

| Output budget | Finish reason | Answer | Thinking |
| --- | --- | --- | --- |
| 64 | length | **empty** | 303 chars |
| 128 | length | **empty** | 550 chars |
| 512 | stop | valid | 1614 chars |

So this is not a quirk of a retired model. Configured budgets (8192-32768) leave headroom
today, which makes it **latent rather than absent** — a context-heavy role on a long prompt
can still land there.

**The preventative flag is model-dependent, and harmful on the wrong model.** The same
request-level instruction to suppress separate reasoning:

| Model | Effect |
| --- | --- |
| Small model A | thinking 600 chars → 0; answer becomes clean, parseable |
| Small model B | same; clean, parseable |
| **The model every role currently uses** | thinking → 0, but **the thinking moves into the answer** (188-298 chars of prose, reproduced twice, does **not** parse) |

On that third model the flag does not remove thinking; it removes the *separation*. Its
baseline behaviour is already correct. Applying the flag there would **manufacture** the
very failure this feature exists to prevent. This is why the policy must be per-model and
default to off.

**Three gaps compound on some roles.** The classification is gated to one role; it reads
prose rather than the structured finish reason; and the reasoning-promotion repair lives in
a proxy layer that four roles bypass entirely. On those roles all three miss at once.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A truncated answer is not called malformed (Priority: P1)

An operator sees a role fail. The report should tell them whether the model produced
garbage or simply ran out of room, because those lead to different actions.

**Why this priority**: It is the smallest change that stops the misdiagnosis, it helps
every role and every model including ones that cannot be configured, and it needs no
model to cooperate. It is also the safety net that makes US2 safe to adopt gradually.

**Independent Test**: Feed a truncated-with-empty-answer failure through the classifier for
a role other than the one it is currently wired to, and confirm it is reported as a
budget/truncation outcome rather than malformed output.

**Acceptance Scenarios**:

1. **Given** a response that finished because it ran out of room and carries no answer,
   **When** any role classifies it, **Then** it is reported as a truncation outcome, not
   as malformed output.
2. **Given** the same response, **When** the role is one that previously received no
   classification at all, **Then** it now receives the same classification as the role that
   already had it.
3. **Given** a response that is genuinely unparseable, **When** it is classified, **Then**
   it is still reported as malformed output.
4. **Given** a response whose finish reason is available as structured data, **When** it is
   classified, **Then** the structured value decides, not the wording of a message.
5. **Given** a backend that reports only a human-readable reason with no structured finish
   reason, **When** it is classified, **Then** the existing phrase matching still applies,
   so no backend loses coverage.
6. **Given** a truncation outcome, **When** the operator reads the report, **Then** it says
   the budget was exhausted rather than implying the model is incapable.

---

### User Story 2 - A model can declare how its reasoning is handled (Priority: P2)

An operator wants a model that supports it to stop emitting separate reasoning, so a
role that needs a strict answer format cannot be starved by thinking.

**Why this priority**: It removes the cause rather than classifying the symptom, and it
covers the roles that bypass the proxy repair. It ranks below US1 because it depends on
per-model evidence and cannot help a model that does not cooperate.

**Independent Test**: Declare a policy on one model, confirm the instruction reaches the
request for roles using that model, and confirm a model without a policy produces a
byte-identical request to today's.

**Acceptance Scenarios**:

1. **Given** a model with no declared policy, **When** any role dispatches to it, **Then**
   the request is unchanged from today.
2. **Given** a model that declares a reasoning policy, **When** a role dispatches to it,
   **Then** the request carries the corresponding instruction.
3. **Given** a role that reaches its model without passing through the proxy layer,
   **When** its model declares a policy, **Then** the policy still applies.
4. **Given** a declared policy naming an unknown option, **When** configuration loads,
   **Then** it is rejected with a message naming the offending value, rather than being
   sent to a model that will ignore or misinterpret it.

---

### User Story 3 - A known-harmful policy cannot be enabled by accident (Priority: P2)

An operator must not be able to quietly turn on, for the model every role depends on, a
setting measured to break it.

**Why this priority**: Equal in importance to US2 and inseparable from it. A per-model
policy without this guard is a loaded gun: the harmful case is the *default* model, and
the failure it causes looks exactly like the bug being fixed, so it would be
misattributed.

**Independent Test**: Confirm the shipped configuration leaves the current model without a
policy, and that the recorded evidence for it says the flag is harmful rather than merely
unset.

**Acceptance Scenarios**:

1. **Given** the shipped configuration, **When** it is inspected, **Then** the model every
   role currently uses has no reasoning policy.
2. **Given** a model whose measured behaviour under a policy is harmful, **When** that is
   recorded, **Then** the record distinguishes "measured harmful" from "not yet measured".
3. **Given** any model that opts in, **When** the opt-in is reviewed, **Then** evidence for
   that specific model exists, because the effect does not generalise between models.

---

### Edge Cases

- A response with an empty answer that finished *normally* (not truncated) is not a
  truncation; it is an empty answer and must stay distinguishable from one that ran out of
  room.
- A response that is both truncated **and** unparseable is reported as truncated, because
  the budget is the actionable cause.
- A backend that reports neither a structured finish reason nor a recognisable phrase must
  fall back to today's behaviour rather than silently claiming truncation.
- A model that accepts the policy instruction but ignores it must not be recorded as
  verified on that basis alone; the observable output is the evidence.
- Retry interacts with truncation: retrying an identical request that was truncated will
  truncate again. A truncation outcome must not be retried unchanged forever.

## Requirements *(mandatory)*

### Functional Requirements

**Classification (US1)**

- **FR-001**: Truncation-with-no-answer MUST be classified distinctly from malformed output
  for **every** role, not only the one role that receives it today.
- **FR-002**: Classification MUST use the structured finish reason when it is available.
- **FR-003**: When no structured finish reason is available, classification MUST fall back
  to the existing phrase matching, so no backend loses coverage it has today.
- **FR-004**: A genuinely unparseable response MUST still be classified as malformed output.
- **FR-005**: An empty answer that finished normally MUST remain distinguishable from one
  that was truncated.
- **FR-006**: A response that is both truncated and unparseable MUST be reported as
  truncated, because the budget is the actionable cause.
- **FR-007**: The operator-facing report for a truncation MUST state that the output budget
  was exhausted, and MUST NOT imply the model is incapable of the task.
- **FR-008**: A truncation outcome MUST NOT be retried indefinitely with an unchanged
  request, since an identical retry truncates identically.

**Per-model reasoning policy (US2)**

- **FR-009**: A model MUST be able to declare a reasoning policy in configuration.
- **FR-010**: A model that declares no policy MUST produce a request identical to today's.
- **FR-011**: A declared policy MUST reach the dispatched request for every role using that
  model, including roles that do not pass through the proxy layer.
- **FR-012**: An unrecognised policy value MUST be rejected when configuration loads,
  naming the offending value, rather than being forwarded to the model.
- **FR-013**: The policy MUST be attached to the model, not to a role or a global setting,
  because the correct choice differs per model.
- **FR-014**: The system MUST NOT require any change to the gateway deployment; the policy
  is carried on the request.

**Safety (US3)**

- **FR-015**: The shipped configuration MUST leave the model all roles currently use
  without a reasoning policy.
- **FR-016**: Evidence for a model MUST distinguish "measured harmful", "measured
  beneficial" and "not yet measured".
- **FR-017**: A test MUST pin that enabling the policy for the known-harmful model is a
  recorded harmful configuration rather than an unexercised default, so the finding cannot
  be lost.

### Key Entities

- **Failure shape**: The classification of a failed response — truncated, empty answer,
  empty body, or malformed body. Already exists; its reach and its inputs change.
- **Reasoning policy**: A model's declared preference for how reasoning is produced.
  Absent by default. Attached to the model.
- **Policy evidence**: The recorded, per-model observation behind an opt-in, including the
  explicitly harmful case.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A truncated-with-no-answer response is reported as truncation for **100%** of
  roles, where today it is one role.
- **SC-002**: No response that is genuinely unparseable changes classification.
- **SC-003**: For every model without a declared policy, the dispatched request is
  unchanged, demonstrated by comparison against today's request.
- **SC-004**: The model all roles currently use ships with no policy, and its harmful
  measurement is recorded and pinned by a test.
- **SC-005**: An operator reading a truncation report can tell it is a budget problem
  without inspecting logs or source.
- **SC-006**: Roles that bypass the proxy layer receive the same protection as roles that
  do not.

## Assumptions

- The classification vocabulary that exists today is correct; only its reach and its inputs
  are wrong. This feature does not redesign it.
- Structured finish-reason data is available on at least some paths. Where it is not, phrase
  matching remains the fallback, so this is additive rather than a replacement.
- The measured per-model effects hold for the models tested. They are explicitly **not**
  assumed to generalise — that non-generalisation is the reason the policy is per-model.
- Configured output budgets are adequate today, so this is a latent-failure fix rather than
  an outage fix. Raising budgets is a separate decision and is out of scope.

## Dependencies

- The existing failure-shape classification and its call sites.
- The existing model catalog, which already resolves a model into dispatch context and is
  the natural carrier for a per-model property.
- Gateway availability for any live verification of a model's behaviour under a policy.

## Out of Scope

- Changing the gateway deployment or its configuration.
- Changing any role's configured output budget.
- Changing which model any role uses.
- Redesigning the retry policy beyond preventing an unchanged infinite retry of a
  truncation (FR-008).
- Opting any model in to a reasoning policy. This feature builds the mechanism and records
  the evidence; enabling it for a model is a separate, evidence-backed decision.
