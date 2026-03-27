# Implementation Plan: General-Purpose Prompt API

**Branch**: `039-general-purpose-prompt-api` | **Date**: 2026-03-27 | **Spec**: [spec.md](./spec.md)

## Summary

Add a `prompt(text, response_format=None)` method to `AssessmentBackendProtocol` and all implementations. This provides a clean API for coordinare nodes that need arbitrary AI responses (classification, reasoning, summarization) without hacking through the card-sufficiency `assess()` method. Then refactor 029's `_classify_with_ai()` to use it.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: anthropic SDK (existing), structlog (existing) -- no new dependencies
**Storage**: N/A -- stateless prompt/response
**Testing**: pytest + pytest-asyncio (existing)
**Scale/Scope**: ~4 modified files, ~1 new protocol method, ~12 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `prompt()` is a clean, single-purpose method; `assess()` remains unchanged |
| II. Testing Discipline | PASS | Each backend + the protocol method independently testable |
| III. User Experience | N/A | Internal API |
| IV. Performance by Design | PASS | Same circuit breaker/retry as assess(); no additional overhead |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/graph/state.py                         # MODIFIED -- add prompt() to AssessmentBackendProtocol
src/coordinare/services/assessment.py                 # MODIFIED -- add prompt() to all backend implementations
src/coordinare/services/claude.py                     # MODIFIED -- add prompt_text() method for raw prompting
src/coordinare/graph/nodes/classify_human_feedback.py # MODIFIED -- refactor _classify_with_ai to use prompt()
tests/unit/test_assessment.py                        # MODIFIED -- prompt() tests for each backend
tests/unit/graph/nodes/test_classify_human_feedback.py # MODIFIED -- update AI classification test mocks
```

## Detailed Implementation Plan

### Step 1 -- Protocol Extension (`state.py`)

Add to `AssessmentBackendProtocol`:
```python
async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]: ...
```

### Step 2 -- Backend Implementations (`assessment.py`, `claude.py`)

**AnthropicApiBackend**: Call `client.messages.create()` with text as a user message. When `response_format="json"`, add "Respond with valid JSON only." to the system prompt. Parse response, return `{"text": raw_text, "data": parsed_json_or_none}`.

**ClaudeCliBackend**: Run `claude --print <text>`, capture stdout. When `response_format="json"`, attempt JSON parse. Return `{"text": stdout, "data": parsed_or_none}`.

**OpenCodeBackend**: Delegate to ClaudeCliBackend pattern (subprocess).

**NoneBackend**: Return `{"text": ""}` immediately.

### Step 3 -- Refactor _classify_with_ai (`classify_human_feedback.py`)

Replace:
```python
result = await assessment_backend.assess(synthetic_card)
raw = result.get("rationale", ...)
```

With:
```python
result = await assessment_backend.prompt(prompt, response_format="json")
parsed = result.get("data")  # already parsed JSON
```

### Step 4 -- Update Tests

Update AI classification test mocks to use `prompt()` return shape instead of `assess()`. Add prompt-specific tests for each backend.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| Protocol method | ~2 LOC | Single method signature addition |
| Backend implementations | ~30 LOC each | Same API call pattern as assess(), different prompt |
| classify_human_feedback refactor | ~10 LOC | Replace assess() hack with prompt() call |
