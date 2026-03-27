# Feature Specification: General-Purpose Prompt API

**Feature Branch**: `039-general-purpose-prompt-api`
**Created**: 2026-03-27
**Status**: Draft

## Overview

The coordinare's `AssessmentBackendProtocol` currently exposes only `assess(card: dict) -> dict`, which is tightly coupled to card sufficiency evaluation. Features like AI feedback classification (029), dry-run reasoning (038), and future AI-powered nodes need a general-purpose prompt interface that accepts arbitrary text and returns structured responses. This feature adds a `prompt(text: str, response_format: str | None = None) -> dict` method to the backend protocol, enabling any coordinare node to leverage the AI backend without shoehorning requests into the card sufficiency shape.

## Clarifications

### Session 2026-03-27

- Q: Should `prompt()` replace `assess()`? -> A: No. `assess()` remains for card sufficiency (it has domain-specific prompt building). `prompt()` is a lower-level method for arbitrary text prompts.
- Q: Should `prompt()` support structured output (JSON mode)? -> A: Yes. An optional `response_format` parameter hints the backend to return structured JSON. When set to "json", the backend adds JSON-mode instructions to the underlying API call.
- Q: Which backends need to implement `prompt()`? -> A: All backends that implement `AssessmentBackendProtocol`: `AnthropicApiBackend`, `ClaudeCliBackend`, `OpenCodeBackend`, and `NoneBackend`. The `NoneBackend` returns a canned empty response.
- Q: Should circuit breaker and retry logic apply? -> A: Yes. `prompt()` goes through the same resilience wrappers as `assess()`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- General-Purpose Prompt Method (Priority: P1)

Any coordinare node can call `assessment_backend.prompt(text)` to send an arbitrary prompt to the AI backend and receive a text response.

**Why this priority**: Without this, features like 029 (AI classification) must hack around the `assess()` contract by passing synthetic cards, which is fragile and misleading.

**Independent Test**: Can be tested by calling `backend.prompt("What is 2+2?")` and verifying a non-empty response is returned.

**Acceptance Scenarios**:

1. **Given** an `AnthropicApiBackend` is configured, **When** `prompt("Classify this: bug in auth")` is called, **Then** a dict with `{"text": "..."}` is returned containing the model's response.
2. **Given** a `ClaudeCliBackend` is configured, **When** `prompt("Summarize this diff")` is called, **Then** the CLI is invoked with `--print <text>` and the stdout is returned.
3. **Given** a `NoneBackend` is configured, **When** `prompt(...)` is called, **Then** `{"text": ""}` is returned (no-op).

---

### User Story 2 -- Structured JSON Response (Priority: P2)

When `response_format="json"` is passed, the backend instructs the AI model to return valid JSON, and the response dict includes a parsed `data` field alongside the raw `text`.

**Why this priority**: AI classification (029) and other structured tasks need JSON responses. Without this, callers must parse JSON from raw text themselves, which is error-prone.

**Acceptance Scenarios**:

1. **Given** `prompt(text, response_format="json")`, **When** the model returns valid JSON, **Then** the response includes `{"text": "...", "data": <parsed>}`.
2. **Given** `prompt(text, response_format="json")`, **When** the model returns invalid JSON, **Then** the response includes `{"text": "...", "data": None}` and a warning is logged.

---

### User Story 3 -- Wire 029 Through prompt() (Priority: P2)

Refactor `_classify_with_ai()` in `classify_human_feedback.py` to use `assessment_backend.prompt(text, response_format="json")` instead of the current synthetic-card hack through `assess()`.

**Why this priority**: Removes the hack and validates the new API end-to-end.

**Acceptance Scenarios**:

1. **Given** the prompt API exists, **When** `_classify_with_ai()` is called, **Then** it calls `prompt()` instead of `assess()` with a synthetic card.
2. **Given** all existing classification tests pass, **Then** backward compatibility is maintained.

---

### Edge Cases

- What if the prompt is empty? (Return `{"text": ""}` immediately, no API call.)
- What if the backend times out during a prompt call? (Same timeout/retry behavior as `assess()`.)
- What if `response_format="json"` but the model returns prose? (Return `data: None`, log warning.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `AssessmentBackendProtocol` MUST add a `prompt(text: str, response_format: str | None = None) -> dict` method.
- **FR-002**: All existing backend implementations MUST implement `prompt()`.
- **FR-003**: `AnthropicApiBackend.prompt()` MUST call the Anthropic Messages API with the provided text as a user message.
- **FR-004**: When `response_format="json"`, the backend MUST add JSON-mode instructions to the API call and attempt to parse the response as JSON, returning it in a `data` field.
- **FR-005**: `ClaudeCliBackend.prompt()` MUST invoke `claude --print <text>` and return `{"text": stdout}`.
- **FR-006**: `NoneBackend.prompt()` MUST return `{"text": ""}` without making any API call.
- **FR-007**: Circuit breaker and retry logic MUST apply to `prompt()` calls identically to `assess()`.
- **FR-008**: An empty prompt MUST return `{"text": ""}` immediately without calling the backend.
- **FR-009**: `_classify_with_ai()` in `classify_human_feedback.py` MUST be refactored to use `prompt()` instead of `assess()` with a synthetic card.

### Key Entities

- **AssessmentBackendProtocol.prompt()**: New method for arbitrary text prompts.
- **PromptResponse**: The return dict shape: `{"text": str, "data": Any | None}`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `backend.prompt("test")` returns a non-empty response on all three backend implementations.
- **SC-002**: `_classify_with_ai()` no longer uses `assess()` with a synthetic card.
- **SC-003**: All existing classification and assessment tests pass unchanged.
- **SC-004**: `response_format="json"` produces parsed JSON in `data` field for valid model output.

## Assumptions

- The Anthropic API supports specifying response format (JSON mode) via the `response_format` parameter or system prompt instructions.
- The `claude` CLI's `--print` flag works for arbitrary prompts, not just card assessments.
- Adding `prompt()` to the protocol does not break existing callers of `assess()` — it's an additive change.
