# Feature Specification: AI Feedback Classification

**Feature Branch**: `029-ai-feedback-classification`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare's `classify_human_feedback` node currently uses keyword heuristics (V1) to classify PR comments into concern categories and route them to the appropriate performer role. This works for obvious keywords but misclassifies ambiguous, multi-concern, or nuanced comments. This feature replaces the keyword-based classifier with an AI-powered classifier that calls the existing assessment backend (Claude) to produce concern categories with confidence scores, while retaining keyword matching as a deterministic fallback when the AI backend is unavailable.

## Clarifications

### Session 2026-03-24

- Q: Should the AI classifier replace or supplement keyword matching? -> A: Replace as the primary path; keyword matching becomes the fallback when the AI backend is unreachable or returns an error.
- Q: What confidence threshold triggers routing? -> A: A concern must have confidence >= 0.6 to be considered actionable. Below that threshold, fall back to keyword matching for that comment.
- Q: Should classification results be cached? -> A: No. Each review cycle re-classifies because comment context may have changed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- AI-Powered Classification (Priority: P1)

When a human posts PR comments, the coordinare sends the comment text to the assessment backend with a classification prompt. The backend returns a list of concern categories with confidence scores. The coordinare routes to the earliest affected performer role based on the highest-confidence concerns.

**Why this priority**: This is the core improvement -- without AI classification, ambiguous comments are misrouted.

**Independent Test**: Can be tested by mocking the assessment backend to return a known classification result and verifying that `performer_stage` is set to the expected role.

**Acceptance Scenarios**:

1. **Given** a PR comment "the auth flow has a vulnerability and the tests are missing", **When** `classify_human_feedback` runs, **Then** the AI classifier returns both `security` (high confidence) and `qa` (high confidence), and `performer_stage` is set to the earliest affected role.
2. **Given** a PR comment "looks good but could use a docstring", **When** `classify_human_feedback` runs, **Then** the AI classifier returns `documentation` with high confidence and `performer_stage` is set to `"documenting"`.
3. **Given** the AI classifier returns `implementation` with confidence 0.9 and `architecture` with confidence 0.4, **When** the coordinare processes the result, **Then** only `implementation` is treated as actionable (architecture is below the 0.6 threshold).

---

### User Story 2 -- Keyword Fallback (Priority: P2)

When the assessment backend is unavailable or returns an error, the coordinare falls back to the existing keyword heuristic classifier. This ensures comments are never silently dropped.

**Why this priority**: Reliability guarantee -- the system must degrade gracefully when the AI backend is down.

**Independent Test**: Can be tested by mocking the assessment backend to raise a connection error and verifying that keyword classification is used instead.

**Acceptance Scenarios**:

1. **Given** the assessment backend is unreachable, **When** `classify_human_feedback` runs with a PR comment containing "bug", **Then** keyword matching classifies it as `implementation` and routing proceeds normally.
2. **Given** the assessment backend returns a malformed response, **When** `classify_human_feedback` runs, **Then** keyword matching is used as fallback and a warning is logged.

---

### Edge Cases

- What if the AI classifier returns zero concerns above the confidence threshold?
- What if the AI classifier returns a concern category not in `CONCERN_TO_STAGE`?
- What if the assessment backend times out mid-request?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `classify_human_feedback` MUST call the assessment backend with a classification prompt containing the PR comment text and the list of valid concern categories.
- **FR-002**: The classification response MUST include a list of `{concern: str, confidence: float}` objects.
- **FR-003**: Only concerns with confidence >= 0.6 MUST be treated as actionable.
- **FR-004**: When no concerns meet the confidence threshold, the node MUST fall back to keyword classification.
- **FR-005**: When the assessment backend is unreachable or returns an error, the node MUST fall back to keyword classification and log a warning.
- **FR-006**: The classification prompt MUST be defined as a module-level constant, not hardcoded inline.
- **FR-007**: The existing `classify_feedback_concerns` function MUST remain available as the fallback classifier.
- **FR-008**: The node MUST log which classification method was used (AI or keyword) for observability.

### Key Entities

- **FeedbackClassification**: A dataclass/TypedDict holding `concern: str` and `confidence: float`.
- **CLASSIFICATION_PROMPT**: The prompt template sent to the assessment backend.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: AI classification is used for 100% of feedback routing when the assessment backend is healthy.
- **SC-002**: Keyword fallback activates within one polling cycle when the assessment backend is unavailable.
- **SC-003**: No existing keyword-classification tests break -- the fallback path is identical to V1 behavior.
- **SC-004**: Classification method (AI vs keyword) is logged on every invocation.

## Assumptions

- The assessment backend (`AssessmentBackendProtocol`) already exists in `CoordinareState` and supports an `assess()` method that can accept arbitrary prompt payloads.
- The confidence threshold (0.6) is hardcoded in V1; a future version may make it configurable.
- Classification adds one API call per review cycle; latency is acceptable because the monitoring loop already includes network calls.
