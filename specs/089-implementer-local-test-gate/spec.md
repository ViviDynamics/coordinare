# Feature Specification: Implementer Local Test Gate

**Feature Branch**: `089-implementer-local-test-gate`  
**Created**: 2026-06-13  
**Status**: Draft  
**Input**: User description: "Implementer local test gate: run detected test_command locally before push with spec-088 env-blocked classification and a bounded coordinare-side self-fix loop"

## Context & Motivation

Today the implementer writes code and pushes it **without ever executing it**. The pre-push gate (`_run_ci_check` in `agent/performer/src/performer/main.py`) runs only the detected *lint* command; the detected *test* command (`ci_detection.detect().test_command`) is ignored. The first time the implementer's code actually runs is GitHub CI — post-push, post-PR — caught by the spec-075 remote CI gate at the implementer→reviewer boundary.

This has two costs:

1. **Expensive feedback loop.** A test failure a local `pytest`/`rspec` would surface in seconds instead costs a full push → CI → gate → bounce → re-dispatch round-trip.
2. **It violates spec-043 (Performer CI Ownership):** "every performer that commits must verify repo CI passes before handoff." Running the linter verifies *style*, not *correctness*.

This feature adds a **local test-execution gate** for the implementer, reusing the spec-088 env-blocked classification so a broken env-cache cannot masquerade as a code defect. It is a *cheap pre-filter in front of* the spec-075 remote gate — it does not replace it. Local pass ≠ remote pass (integration tests, matrix builds, and services not present in the env-cache still only run remotely), so spec-075 remains the authority.

### Relationship to existing gates

| Gate | Spec | When | Authority |
|------|------|------|-----------|
| Pre-push lint | 043 | Before push, in performer | Style only |
| **Local test gate** | **089 (this)** | **Before push, in performer** | **Cheap correctness pre-filter** |
| Remote CI gate | 075 | After push, coordinare graph | Authoritative (integration, matrix) |
| Closer PR-checks gate | 064 | At close, coordinare graph | Final |

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Implementer catches its own test failures before push (Priority: P1)

The implementer finishes writing code. Before pushing, the local test command runs inside the performer (env-cache active). If tests fail for a code reason, the implementer does not push broken code — it gets the failing test output as feedback and tries again, all without a remote round-trip.

**Why this priority**: This is the core value — keep known-red code out of the PR, and shorten the feedback loop from minutes (push→CI) to seconds (local run). Delivers a viable MVP on its own.

**Independent Test**: With the gate enabled on a repo whose `test_command` is detectable, hand the implementer a change that breaks a test. Verify it does NOT open a PR with red tests, and that the failing test output is surfaced as `changes_requested` feedback.

**Acceptance Scenarios**:

1. **Given** the gate is enabled and `test_command` is detected, **When** the implementer declares done and local tests pass, **Then** lint runs, tests run green, the branch is pushed, and the PR is opened (existing path, unchanged).
2. **Given** the gate is enabled and local tests fail for a code reason, **When** the implementer declares done, **Then** no push occurs and the response is `changes_requested` carrying the failing test output.
3. **Given** the gate is enabled but no `test_command` is detected for the stack, **When** the implementer declares done, **Then** the test step is skipped (pass-through) exactly as the lint gate skips when no linter is detected.

---

### User Story 2 - A broken env-cache is classified as env-blocked, not a code defect (Priority: P1)

When local tests fail because the env-cache failed to bootstrap or its services failed to start — not because the code is wrong — the implementer must NOT treat that as a code defect. It must bail immediately to the blocked column with an env-cache reason, without consuming any self-fix attempts and without re-prompting the agent to "fix" code that is fine.

**Why this priority**: Without this, the local gate would reproduce the exact failure mode spec-088 fixed for QA — env failures misattributed as code failures, sending the agent in circles. Co-equal P1 with US1; the gate is unsafe to ship without it.

**Independent Test**: With the gate enabled, simulate a recorded services-start / env-cache-health failure (the spec-088 signals) coinciding with a local test failure. Verify the outcome is env-blocked (routed to blocked with an env-cache reason), the self-fix attempt counter is NOT incremented, and the agent is NOT re-dispatched as `changes_requested`.

**Acceptance Scenarios**:

1. **Given** a recorded env-cache services-start failure (`consume_services_start_failure()`), **When** local tests fail, **Then** the failure is classified env-blocked and the card is routed to blocked with the env-cache reason — not `changes_requested`.
2. **Given** a recorded env-cache health failure (`consume_env_cache_health_failure()`), **When** local tests fail, **Then** the same env-blocked classification applies.
3. **Given** local tests fail with NO env-cache failure signal present, **When** the gate evaluates, **Then** the failure is treated as a genuine code defect (US1 path).

---

### User Story 3 - Bounded self-fix loop with escalation, never push known-red code (Priority: P2)

When local tests fail for a code reason, the implementer iterates: in-session first (the agent runs and fixes tests within its own turn), then via a bounded coordinare-side re-dispatch backstop. After the attempt budget is exhausted and tests are still red, the card escalates to the blocked column with the failing test output as the blocking reason. Known-red code is never pushed.

**Why this priority**: Builds on US1 to make the loop converge or fail loudly rather than thrash forever or silently give up. P2 because US1 already delivers value with even a single attempt; the budget/escalation refines it.

**Independent Test**: With `max_fix_attempts` set to a small N, hand the implementer a test failure it cannot fix. Verify it re-dispatches exactly N times and then escalates to blocked (carrying the test output) without ever pushing.

**Acceptance Scenarios**:

1. **Given** local tests fail for a code reason and attempts remain in budget, **When** the gate evaluates, **Then** the per-head self-fix attempt counter increments and the implementer is re-dispatched with the failing output.
2. **Given** the per-head self-fix budget is exhausted and tests are still red, **When** the gate evaluates, **Then** the card escalates to blocked with the failing test output as the blocking reason — and no push occurs.
3. **Given** the local self-fix counter, **When** counting attempts, **Then** it is tracked separately from spec-075's `max_bounces_per_head` (cheap local retries are not conflated with expensive remote bounces).

---

### Edge Cases

- **No test command detected** → skip the test step (pass-through), like the lint gate. The gate is a no-op for stacks with no detectable test runner.
- **Test run times out** → classified as a genuine failure (US1/US3 path), NOT env-blocked, *unless* an env-cache failure signal is also present. The timeout must be generous (configurable, default 600s) so slow suites are not mistaken for hangs.
- **Coordinare package unavailable (standalone performer)** → gate degrades gracefully to pass-through (same fallback `_run_ci_check` already uses); the spec-075 remote gate is the backstop.
- **Gate disabled (legacy symphony / opt-out)** → behaviour is identical to today: lint only, then push.
- **Lint fails** → existing behaviour wins first; tests only run after lint passes (lint is cheaper and a lint failure already blocks push).
- **Tests pass locally but fail remotely** (env mismatch, integration-only tests) → spec-075 catches it post-push; expected and acceptable — local is a pre-filter, not a guarantee.
- **Env-cache failure detected but tests actually pass** → push proceeds; env-blocked is only relevant when it would otherwise misclassify a *failure*.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The performer MUST provide a role-agnostic helper that runs the detected `test_command` (via `ci_detection.detect()`) in the workspace and returns a structured result (passed / output / env-blocked / env-reason). When no `test_command` is detected, the helper MUST return pass-through.
- **FR-002**: The implementer done-path MUST invoke the local test gate **after** the lint gate passes and **before** pushing the branch.
- **FR-003**: When local tests pass (or are skipped), the implementer MUST proceed to the existing push + open-PR path unchanged.
- **FR-004**: When local tests fail for a code reason (no env-cache failure signal), the implementer MUST NOT push, and MUST return `changes_requested` carrying the failing test output.
- **FR-005**: When local tests fail AND an env-cache failure signal is present (`consume_services_start_failure()` or `consume_env_cache_health_failure()`), the implementer MUST classify the outcome as **env-blocked** and route the card to the blocked column with the env-cache reason — not `changes_requested`.
- **FR-006**: An env-blocked classification MUST NOT increment the self-fix attempt counter and MUST NOT re-dispatch the agent to fix code.
- **FR-007**: The system MUST track a per-head **self-fix attempt counter** for local-test failures, separate from spec-075's `max_bounces_per_head`.
- **FR-008**: When the self-fix budget (`max_fix_attempts`) is exhausted and local tests are still red, the system MUST escalate the card to the blocked column with the failing test output as the blocking reason, and MUST NOT push.
- **FR-009**: The gate MUST be opt-in via symphony configuration (`local_test_gate` with at least `enabled`, `timeout_seconds`, `max_fix_attempts`), defaulting to **disabled** so legacy symphonies are unaffected.
- **FR-010**: The local test run MUST use a configurable timeout (default 600s). A timeout MUST be classified as a genuine failure unless an env-cache failure signal is also present.
- **FR-011**: The implementer role contract/prompt MUST direct the agent to run `test_command` and fix failures within its own turn before declaring done (in-session self-fix as the primary layer; the coordinare verify is the backstop).
- **FR-012**: When the coordinare package is unavailable to the performer (standalone mode), the gate MUST degrade gracefully to pass-through, relying on the spec-075 remote gate as backstop.
- **FR-013**: The gate MUST emit structured observability for each decision (pass / code-fail-redispatch / env-blocked / escalate), including the detected command, duration, attempt count, and classification.

### Key Entities

- **LocalTestResult**: outcome of one local test run — `passed: bool`, `output: str`, `env_blocked: bool`, `env_reason: str | None`, `command: str | None`, `duration_seconds: float`.
- **Local-test self-fix attempt counter**: per-head-SHA integer, persisted in coordinare/session state alongside (but distinct from) spec-075's `bounce_counter`.
- **`local_test_gate` config**: symphony-scoped opt-in — `enabled: bool` (default false), `timeout_seconds: int` (default 600), `max_fix_attempts: int` (default 2).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the gate enabled, 0% of implementer pushes carry tests that fail locally for a code reason (known-red code is never pushed).
- **SC-002**: A code-reason test failure is surfaced to the implementer locally (no push) rather than via a remote CI bounce in 100% of cases where `test_command` is detectable.
- **SC-003**: 100% of local test failures coinciding with an env-cache failure signal are classified env-blocked (routed to blocked), with 0 self-fix attempts consumed and 0 erroneous `changes_requested` re-dispatches.
- **SC-004**: Local self-fix attempts are counted independently from spec-075 remote bounces — the two budgets never share or deplete each other.
- **SC-005**: With the gate disabled (default), implementer behaviour is byte-for-byte identical to pre-089 (lint, then push) — verified by existing tests remaining green with no config change.
