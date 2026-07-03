# Feature Specification: Coordinare Pipeline Flow Optimizations

**Feature Branch**: `123-pipeline-flow-optimizations`
**Created**: 2026-06-30
**Status**: Draft

## Overview

Seven concrete inefficiencies in the coordinare performer pipeline identified through live analysis. Each is independently shippable. The optimizations reduce redundant AI dispatches, eliminate no-op doc commits, prevent false `blocked` escalations caused by infrastructure failures, and sharpen role boundaries between reviewer, QA, and closer.

## User Scenarios & Testing

### User Story 1 — Tech_writer doc-change gate (Priority: P1)

The tech_writer (documenting stage) runs unconditionally after every QA pass, even when the PR touches zero documentation files. This produces no-op doc commits that pollute the branch history and waste a full performer dispatch + LLM call.

**Why this priority**: Highest frequency win. Styling-only, copy, or bug-fix PRs never need doc regeneration. A simple gate eliminates ~30-50% of tech_writer dispatches with no behaviour change for PRs that do change docs.

**Independent Test**: Given a PR that touches only non-doc source files, when the QA stage passes, then the documenting stage is skipped — no performer dispatched, stage logged as `skipped`, branch has no new doc commits.

**Acceptance Scenarios**:

1. **Given** a PR whose diff contains no `docs/` file changes, **When** the QA stage passes and coordinare evaluates the documenting stage, **Then** tech_writer is NOT dispatched, the stage is logged as `skipped: no_doc_changes`, and the card advances to closed.
2. **Given** a PR that adds or modifies a `docs/` file, **When** QA passes, **Then** tech_writer IS dispatched normally.
3. **Given** a PR with no doc changes where the last tech_writer run already generated identical content, **When** QA passes, **Then** tech_writer is skipped even if `docs/` files exist (content hash unchanged).

---

### User Story 2 — QA persona: acceptance-criteria only (Priority: P2)

The QA persona currently duplicates the reviewer persona's code-quality work (linting, test coverage checks). QA should focus exclusively on verifying acceptance criteria from the card description, capturing visual evidence (screenshots), and confirming the feature works end-to-end. Code quality is already reviewed; QA re-doing it wastes 15-20 minutes per cycle.

**Why this priority**: Directly reduces QA stage duration and token cost. Clear role boundary prevents ambiguous `changes_requested` signals.

**Independent Test**: Given a PR that passes reviewer, when QA runs, then QA only checks acceptance criteria and visual evidence — it does not run linters, does not check test coverage percentages, does not report code style issues.

**Acceptance Scenarios**:

1. **Given** a PR with a linting warning that reviewer did not flag, **When** QA runs, **Then** QA does NOT return `changes_requested` solely for the lint warning (outside QA scope).
2. **Given** a PR with a failing acceptance criterion (e.g., a feature button doesn't appear), **When** QA runs, **Then** QA returns `changes_requested` citing the specific criterion failure.
3. **Given** a PR that meets all acceptance criteria and QA captures at least one screenshot, **When** QA runs, **Then** QA returns `qa_passed`.

---

### User Story 3 — Split bounce budget: content vs. infrastructure (Priority: P3)

The single `feedback_cycle_count` counter treats transient infrastructure failures (env_blocked, container crash, system_error) identically to genuine content feedback (reviewer `changes_requested`). A card hitting 3 infrastructure stalls + 2 reviewer bounces exhausts the 5-cycle limit and enters `blocked` as "unresolvable" when the real problem was environment flakiness.

**Why this priority**: Prevents incorrect `blocked` escalations. Clears cards that have legitimate content feedback remaining but used up budget on infra failures.

**Independent Test**: Given a card that has hit `env_blocked` 3 times and `changes_requested` 2 times, when the bounce budget is evaluated, then the card is NOT blocked on content grounds (content budget = 2/5, still has room).

**Acceptance Scenarios**:

1. **Given** a card with 5 infrastructure failures and 0 content feedback cycles, **When** bounce budget is evaluated, **Then** the infra budget is exhausted → card enters `env_blocked` hold (spec-095 path), not general `blocked`.
2. **Given** a card with 5 `changes_requested` feedback cycles and 0 infrastructure failures, **When** bounce budget is evaluated, **Then** the content budget is exhausted → card enters `blocked` with `reason=feedback_cycle_limit`.
3. **Given** existing `coordinare.state.json` with legacy `feedback_cycle_count=3` and no `content_feedback_cycles`, **When** coordinare restarts, **Then** `content_feedback_cycles` reads the legacy value — no data lost, no reset.

---

### User Story 4 — Assessor Q&A carryover across bounce cycles (Priority: P4)

When a card bounces and the assessor is re-dispatched, it receives no memory of prior clarification Q&A. It re-asks the same open questions that were already answered, wasting a full round-trip per bounce.

**Why this priority**: Direct time savings on multi-bounce cards. Assessor re-asking known answers is the most visible sign of coordinare "forgetting."

**Independent Test**: Given a card that bounced after the assessor already recorded `open_questions` answers, when the assessor is dispatched again, then the prior answers appear in the assessor's context as `prior_clarifications` and it does NOT re-ask them.

**Acceptance Scenarios**:

1. **Given** a card with persisted `open_questions` from a prior assessor run, **When** the assessor is re-dispatched on a bounce, **Then** `prior_clarifications` in the assessor's card context contains the prior Q&A.
2. **Given** a card on its first assessor dispatch (no prior Q&A), **When** the assessor is dispatched, **Then** `prior_clarifications` is absent or empty — no regression.

---

### User Story 5 — Multi-concern feedback routes through assessor (Priority: P5)

When reviewer returns `changes_requested` feedback covering 2 or more distinct concern categories (e.g., architecture + implementation + style), the card routes directly to implementing. For multi-concern feedback, scope re-confirmation via the assessor reduces the risk of the implementer addressing the wrong concerns.

**Why this priority**: Reduces multi-bounce loops caused by scope misalignment on complex feedback. Lower priority because it adds a stage for complex cases only.

**Independent Test**: Given reviewer feedback classified as touching 2+ distinct concern categories, when coordinare routes the card after reviewing, then the card routes to assessing (not implementing) for scope re-confirmation.

**Acceptance Scenarios**:

1. **Given** reviewer feedback with concerns classified as `[architecture, implementation]`, **When** coordinare determines next stage, **Then** card routes to `assessing` with the multi-concern feedback injected as context.
2. **Given** reviewer feedback with only a `[style]` concern, **When** coordinare determines next stage, **Then** card routes directly to `implementing` (no assessor round-trip).
3. **Given** reviewer feedback with the same concern category repeated (e.g., two implementation issues), **When** coordinare determines next stage, **Then** card routes directly to `implementing` (not 2 distinct categories).

---

### User Story 6 — Dedup comments before AI classification (Priority: P6)

`_classify_with_ai()` is called for all pending review comments on every board poll cycle. Already-processed comment IDs are filtered after classification, meaning the same comments are re-classified on consecutive polls. Each re-classification costs an AI call that produces an identical result.

**Why this priority**: Pure efficiency win with no behaviour change — just moves the dedup filter earlier.

**Independent Test**: Given a board poll where 3 comments were already classified in the prior cycle, when comment classification runs, then `_classify_with_ai()` is called 0 times — not 3 times.

**Acceptance Scenarios**:

1. **Given** 5 comments on a PR where 3 are already in `processed_comment_ids`, **When** classification runs, **Then** `_classify_with_ai()` is called at most 2 times (for the 2 unprocessed comments only).
2. **Given** 5 comments where all 5 are already processed, **When** classification runs, **Then** `_classify_with_ai()` is called 0 times and the stage completes immediately.
3. **Given** a fresh PR with 3 new comments (none processed), **When** classification runs, **Then** all 3 comments are classified — no regression on new comments.

---

### User Story 7 — Closer as lightweight thread-resolution verifier (Priority: P7)

The closing_review stage uses a persona that reads the full PR diff and performs code review — the same work reviewer already did. The closer persona says "don't redo deep review" but includes CI linter checks. Redefining closer as a thread-resolution verifier (confirm open reviewer threads are addressed; no diff re-review, no linting) makes it a fast stamp step.

**Why this priority**: Lowest risk change (persona update only), clears role ambiguity, speeds the closing stage.

**Independent Test**: Given a PR where reviewer threads are all marked resolved, when closer runs, then closer approves without re-running linters or repeating code review — it only verifies thread resolution.

**Acceptance Scenarios**:

1. **Given** a PR with all reviewer threads marked resolved and CI passing, **When** closer runs, **Then** closer returns `approved` without inspecting code quality.
2. **Given** a PR with an unresolved reviewer thread, **When** closer runs, **Then** closer returns `changes_requested` citing the unresolved thread (not a code quality issue).
3. **Given** a PR with a lint warning that reviewer did not flag, **When** closer runs, **Then** closer does NOT return `changes_requested` for the lint warning (outside closer scope).

---

### Edge Cases

- What happens when a PR diff is entirely binary/image files (no text `docs/` changes)? → Doc gate treats it as no-doc-change → skip tech_writer.
- What happens when `feedback_cycle_count` is absent from legacy state? → Both new budget counters default to 0.
- What happens when reviewer feedback has no classifiable concern categories? → Route directly to implementing (safe default, no assessor round-trip).
- What happens when the comment dedup filter removes all comments before classification? → Stage completes immediately as a no-op; no error.
- What happens when a card's `transient_error_cycles` budget is exhausted but `content_feedback_cycles` is still under limit? → Card takes the spec-095 ENV_BLOCKED hold path (not general blocked), preserving its content feedback budget for when the environment is fixed.

## Requirements

### Functional Requirements

**US1 — Tech_writer doc-change gate**

- **FR-001**: Before dispatching the tech_writer, coordinare MUST check whether the PR diff contains changes to any `docs/` file path.
- **FR-002**: If no `docs/` file changes exist in the PR diff, coordinare MUST skip the documenting stage and log `stage_skipped: reason=no_doc_changes`.
- **FR-003**: If content-hash dedup is available (`doc_dedup.py`), coordinare MUST additionally skip tech_writer when all doc changes are hash-identical to the last committed doc content.

**US2 — QA persona scope**

- **FR-004**: The QA performer persona MUST NOT instruct the performer to run linters, check test coverage percentages, or report code style issues.
- **FR-005**: The QA performer persona MUST instruct the performer to verify each acceptance criterion from the card description and capture at least one visual evidence artifact.

**US3 — Split bounce budget**

- **FR-006**: Coordinare MUST track `content_feedback_cycles` (incremented on `changes_requested` from reviewer or QA) separately from `transient_error_cycles` (incremented on `env_blocked`, `system_error`, or `unknown` backend failures).
- **FR-007**: The `content_feedback_cycles` exhaustion limit MUST default to 5 (matching the prior `feedback_cycle_limit`).
- **FR-008**: The `transient_error_cycles` exhaustion limit MUST default to 3 per card (per-card infrastructure failure budget, separate from the per-dispatch retry budget in monitor_performer).
- **FR-009**: On coordinare restart, if `feedback_cycle_count` exists on a persisted session and `content_feedback_cycles` is absent, coordinare MUST treat the legacy value as `content_feedback_cycles` (backward-compatible migration).

**US4 — Assessor Q&A carryover**

- **FR-010**: Coordinare MUST persist answered `open_questions` on the card's session state after each assessor run completes.
- **FR-011**: When re-dispatching the assessor on a bounce cycle, coordinare MUST include the persisted prior answers as `prior_clarifications` in the card context payload.

**US5 — Multi-concern assessor gate**

- **FR-012**: Coordinare MUST classify reviewer `changes_requested` feedback into concern categories (architecture, implementation, style, tests) using the existing comment classification infrastructure.
- **FR-013**: If reviewer feedback spans 2 or more distinct concern categories, coordinare MUST route the card to the assessing stage before implementing, with the feedback injected as context.
- **FR-014**: If reviewer feedback spans only 1 concern category (or categories cannot be determined), coordinare MUST route directly to implementing (no assessor round-trip).

**US6 — Comment dedup before classification**

- **FR-015**: Before calling the AI comment classifier, coordinare MUST filter out any comment IDs already present in `processed_comment_ids`.
- **FR-016**: If all pending comments are already processed after filtering, coordinare MUST skip the AI classification call entirely for that poll cycle.

**US7 — Closer persona scope**

- **FR-017**: The closing_review performer persona MUST NOT instruct the performer to re-run linters, re-examine code quality, or repeat diff review already done by the reviewer.
- **FR-018**: The closing_review performer persona MUST instruct the performer to verify that open reviewer threads are resolved and that CI is passing before approving.

### Key Entities

- **`content_feedback_cycles`** (PersistedSession field): Count of `changes_requested` bounces on a card. Replaces legacy `feedback_cycle_count` for content-driven exhaustion. Default: 0. Migration: reads from `feedback_cycle_count` if present.
- **`transient_error_cycles`** (PersistedSession field): Count of infrastructure-driven failures (env_blocked, system_error, unknown) on a card. Independent from content cycles. Default: 0.
- **`open_questions`** (PersistedSession field): Persisted Q&A answers from assessor runs. Passed as `prior_clarifications` on re-dispatch. Default: empty.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Tech_writer is not dispatched for at least 30% of QA-passing cards in a live run where those cards have no `docs/` file changes.
- **SC-002**: QA stage duration decreases by at least 15% on cards where the prior bottleneck was linting/test-coverage re-checking (measured by performer runtime).
- **SC-003**: Zero cards enter `blocked` solely due to infrastructure failures when their `content_feedback_cycles` count is below the exhaustion limit.
- **SC-004**: Assessors do not re-ask a question that was already answered and persisted in the prior cycle (verifiable by inspecting card context payloads in logs).
- **SC-005**: Cards with multi-concern reviewer feedback have their next stage as `assessing` in at least 95% of eligible cases.
- **SC-006**: AI classification calls per board poll cycle decrease by at least 50% after the first poll cycle (dedup filter eliminates re-classification of already-processed comments).
- **SC-007**: Closer stage performer runtime decreases by at least 30% compared to pre-change baseline.

## Assumptions

- `doc_dedup.py` exists in the codebase and has a callable content-hash API; FR-003 depends on this. If the API is not straightforwardly callable, FR-003 is deferred (FR-001/002 provide the core gate).
- Concern category classification for FR-012 reuses existing comment classification infrastructure without requiring a new AI call.
- `processed_comment_ids` is an existing field on card session state that is populated after successful classification; FR-015 assumes this field is present.
- The `transient_error_cycles` per-card budget (FR-008, limit=3) is distinct from the per-dispatch backend retry count in `monitor_performer` (spec-098, which retries up to 3 times per dispatch). The new field counts across dispatches.
- All 7 user stories are independently shippable; no US depends on another US being complete first.
