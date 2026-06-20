# Feature Specification: Infrastructure/Environment CI-Failure Classification

**Feature Branch**: `095-env-blocked-ci`  
**Created**: 2026-06-19  
**Status**: Draft  
**Input**: User description: "Infrastructure/environment CI-failure classification — surface, don't repair-or-bounce. Continues the spec-090 baseline-repair line."

## Overview

Spec 090 classifies a card's failing CI checks as INHERITED, INTRODUCED, FLAKE, or UNKNOWN, and may autonomously repair INHERITED failures. But some CI failures are caused by neither the card nor the codebase — they are **infrastructure/environment** failures that no code change can fix: the CI artifact-storage quota is exhausted (the upload step fails while the tests themselves pass), a self-hosted runner is offline, a billing/spending-limit blocks a job, or a required job fails on an external dependency outage. Under 090, such a failure is mislabeled (e.g. INHERITED, because it fails identically on the merge base) and coordinare either fails to "repair" it or re-dispatches the performer against it indefinitely.

This was observed live on the website symphony (PR #159 / card #158): the feature tests passed with zero failures, but the job failed on an org-wide artifact-storage-quota block. With no category for "no code change can fix this," coordinare bounced the performer for roughly nine days against a failure no performer could resolve, while the actual fix was an operator raising a storage budget.

This feature adds an ENV_BLOCKED classification that is evaluated before repair eligibility: an infra/environment failure is recognized by signature, the card is held in an actionable operator-visible state (no repair, no bounce), the specific cause and needed action are surfaced once to the operator, and normal flow resumes automatically when the condition clears.

## Clarifications

### Session 2026-06-19

- Q: Should ENV_BLOCKED be evaluated before or after 090's inherited/introduced classification? → A: Before repair eligibility — an infra failure is never a repair candidate, so it must short-circuit the inherited/introduced path.
- Q: How is an infra failure recognized? → A: By a signature match against a known + operator-extensible (config-driven) set of reason patterns; an unrecognized failure is never labeled ENV_BLOCKED (fail-safe fallthrough to existing 090 logic).
- Q: Does coordinare try to fix the infrastructure (raise budgets, clear artifacts, provision runners)? → A: No. Coordinare surfaces the condition and the needed action; the operator acts. Auto-remediation is out of scope.
- Q: How does ENV_BLOCKED differ from FLAKE? → A: FLAKE is transient and retried; ENV_BLOCKED is a stable, recurring condition that persists until the operator acts, so it is held (not retried on a timer) and resumes automatically once the signature clears.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Hold and surface an infra-blocked card instead of bouncing (Priority: P1)

A card's PR has green tests, but a required CI job fails because the org's CI artifact-storage quota is exhausted (or a runner is offline). Coordinare recognizes this as an environment failure, stops re-dispatching the performer, and tells the operator exactly what is wrong and what to do.

**Why this priority**: This is the exact 9-day-bounce failure on #159 — the highest-severity waste, where coordinare burns performer cycles (and money) against a failure no code change can fix, while the operator has no clear signal of the real cause. It is the core value of the feature.

**Independent Test**: Present a card whose CI shows a required check failing with an infra-signature reason (e.g. artifact-storage-quota) while its tests pass; confirm coordinare labels it ENV_BLOCKED, does not dispatch a performer or attempt a repair, and records an operator-visible hold naming the infra cause and needed action.

**Acceptance Scenarios**:

1. **Given** a card whose required CI check fails with a recognized infra signature (artifact-storage quota), **When** classification runs, **Then** the failure is labeled ENV_BLOCKED, no autonomous repair is attempted, and the performer is not re-dispatched.
2. **Given** an ENV_BLOCKED card, **When** coordinare reaches the point where it would otherwise bounce/re-dispatch, **Then** it instead holds the card in an actionable state and does not consume a performer dispatch.
3. **Given** a failure whose signature is NOT recognized as infra, **When** classification runs, **Then** it is NOT labeled ENV_BLOCKED and falls through to the existing INHERITED/INTRODUCED/FLAKE/UNKNOWN handling unchanged.

---

### User Story 2 - Tell the operator the real cause, once (Priority: P1)

When a card is ENV_BLOCKED, the operator receives a notification that names the specific infrastructure cause and the action needed — not a generic "tests failed" — and is not re-spammed every cycle.

**Why this priority**: Surfacing is the whole point — an infra failure can only be resolved by a human, so the signal must be specific and actionable. Equal priority to US1 because holding without a clear signal just trades a loud failure for a silent one.

**Independent Test**: Trigger an ENV_BLOCKED classification and confirm exactly one operator notification is emitted naming the cause (e.g. "Actions artifact-storage quota exhausted — raise the storage budget or clear old artifacts") and that subsequent cycles in the same blocked state do not re-notify.

**Acceptance Scenarios**:

1. **Given** a newly ENV_BLOCKED card, **When** coordinare surfaces it, **Then** the operator notification names the specific infra cause and the suggested operator action (distinct from a generic test-failure message).
2. **Given** a card that remains ENV_BLOCKED across multiple cycles, **When** coordinare re-evaluates, **Then** it does not re-notify for the same condition (deduped to once per condition).
3. **Given** any ENV_BLOCKED notification or record, **When** its contents are inspected, **Then** it contains only check names, conclusions, normalized reasons, and identifiers — never secret values.

---

### User Story 3 - Auto-resume when the infra condition clears (Priority: P2)

After the operator raises the budget (or the runner comes back), the next CI run no longer shows the infra signature. Coordinare detects this and resumes normal flow — re-evaluating and dispatching — with no manual reset.

**Why this priority**: Closes the loop so the hold is self-clearing. Valuable but secondary to recognizing and surfacing the block in the first place.

**Independent Test**: Take an ENV_BLOCKED card, then present a subsequent CI result with no infra signature; confirm coordinare clears the ENV_BLOCKED state and resumes normal classification/dispatch automatically.

**Acceptance Scenarios**:

1. **Given** an ENV_BLOCKED card whose next CI evaluation no longer matches any infra signature, **When** coordinare re-evaluates, **Then** the ENV_BLOCKED hold is cleared and normal flow resumes without operator intervention.
2. **Given** the cleared card, **When** it resumes, **Then** its subsequent failures (if any) are classified by the normal INHERITED/INTRODUCED/FLAKE/UNKNOWN logic.

---

### Edge Cases

- **Mixed failures**: a card has both an infra-blocked required check AND a genuinely introduced code failure. ENV_BLOCKED on one check must not mask the introduced failure on another — the introduced failure is still classified/handled; the card is held on the infra block but the operator signal distinguishes both.
- **Infra signature on a non-required check**: a non-required check failing on an infra signature must not hold the card (mirror the required-set policy from 090 L1).
- **Signature ambiguity**: a reason that partially matches an infra pattern but is actually a code failure must fail safe to NOT-ENV_BLOCKED (conservative; never withhold dispatch it cannot justify).
- **Flapping infra**: the condition clears then recurs across cycles — resume on clear, re-block (and re-notify once) on recurrence, without thrash.
- **Baseline indeterminate**: the merge-base/baseline can't be read — ENV_BLOCKED signature detection still applies on the head failure alone (it does not depend on the baseline), but no INHERITED labels are produced (per 090 fail-safe).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST add an ENV_BLOCKED classification for a failing **required** CI check whose failure signature matches a known infrastructure/environment pattern, evaluated **before** inherited/introduced repair eligibility.
- **FR-002**: ENV_BLOCKED detection MUST be signature-based and operator-extensible via configuration (a list of reason patterns), shipping with a built-in set covering at least: CI artifact-storage quota exhaustion, runner offline/unavailable, and billing/spending-limit blocks.
- **FR-003**: A failure that matches no infra signature MUST NOT be labeled ENV_BLOCKED and MUST fall through to the existing INHERITED/INTRODUCED/FLAKE/UNKNOWN handling unchanged (fail-safe).
- **FR-004**: An ENV_BLOCKED card MUST NOT trigger autonomous repair and MUST NOT re-dispatch/bounce the performer; coordinare holds the card in an actionable, operator-visible state and consumes no performer dispatch for that condition.
- **FR-005**: The system MUST surface an ENV_BLOCKED card to the operator via the existing notification channel(s), naming the specific infra cause and the suggested operator action (distinct from a generic test-failure message).
- **FR-006**: ENV_BLOCKED operator notifications MUST be deduplicated to once per condition (no re-notification each cycle while the same block persists).
- **FR-007**: The system MUST treat ENV_BLOCKED as a stable, persisting condition (not a flake): it is held until cleared, not retried on a timer, and is distinct from FLAKE.
- **FR-008**: When a subsequent CI evaluation no longer matches any infra signature, the system MUST clear the ENV_BLOCKED hold and resume normal classification/dispatch automatically, without manual intervention.
- **FR-009**: An ENV_BLOCKED block on one check MUST NOT mask an INTRODUCED failure on another check of the same card; the introduced failure is still classified and the operator signal distinguishes the two.
- **FR-010**: All ENV_BLOCKED state, decisions, notifications, and logs MUST contain only check names, conclusions, normalized reasons, and identifiers — never secret values.
- **FR-011**: ENV_BLOCKED classification MUST be evaluated on the head failure alone and MUST NOT depend on a readable baseline (so it functions even when the baseline is indeterminate).
- **FR-012**: The feature MUST be opt-in / config-gated and default-off, leaving pre-feature routing/verdicts unchanged when disabled.

### Key Entities *(include if feature involves data)*

- **Failure Signature**: the stable `(check name, conclusion, normalized reason)` descriptor already used by 090 classification; the basis for matching infra patterns.
- **Infra Signature Pattern**: a configured/built-in reason pattern (e.g. artifact-storage-quota, runner-offline, billing-limit) plus the operator-facing cause + suggested action it maps to.
- **ENV_BLOCKED State**: per-card state on the existing session recording that the card is held on an infra block, which condition, and whether the operator has been notified (for dedup). Cleared when the signature no longer matches.
- **Operator Notification**: the deduped, secret-free message naming the infra cause and needed action, emitted via the existing Slack/GitHub-comment channel.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card whose only failing required check is an infra/environment block consumes **zero** performer re-dispatches for that condition (no bounce), versus repeated re-dispatch today.
- **SC-002**: 100% of recognized infra failures are surfaced to the operator with a specific cause + action, not a generic test-failure message.
- **SC-003**: An ENV_BLOCKED condition is announced to the operator **once** per condition, not once per cycle.
- **SC-004**: After the infra condition clears, the card resumes normal flow within one evaluation cycle, with no manual reset.
- **SC-005**: An unrecognized failure is never labeled ENV_BLOCKED (zero false ENV_BLOCKED holds in the test corpus of non-infra failures), so no card is wrongly withheld from repair/dispatch.
- **SC-006**: With the feature disabled, routing, verdicts, and notifications are identical to the pre-feature baseline.
- **SC-007**: The #159 scenario (tests pass, artifact-storage quota exhausted) is classified ENV_BLOCKED and surfaced with the storage-budget action, instead of bouncing.

## Assumptions

- Coordinare already reads each card's CI check rollup (names, conclusions, and a normalized reason) at the existing gate — this feature consumes that signal; it adds no new external dependency.
- The existing 090 failure-signature mechanism (`name, conclusion, normalized reason`) is the right substrate for infra-pattern matching; this feature extends classification, it does not replace 090's inherited/introduced/flake logic.
- The state model remains the existing single-host single-process JSON snapshot; ENV_BLOCKED is per-card session state.
- The operator notification channels (Slack / GitHub comment) already exist and are reused.

## Out of Scope

- Auto-remediating the infrastructure itself (raising budgets, clearing artifacts, provisioning/restarting runners) — coordinare surfaces; the operator acts.
- Changing 090's INHERITED/INTRODUCED/FLAKE/UNKNOWN classification logic.
- GitHub Actions billing / storage-SKU configuration (operator-owned).
- Auto-merge or any change to the human-approval requirement.
