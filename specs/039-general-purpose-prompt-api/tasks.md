# Tasks: General-Purpose Prompt API (039)

## Protocol Extension

- [x] Add `prompt(text: str, response_format: str | None = None) -> dict[str, Any]` to `AssessmentBackendProtocol` in `state.py`
- [x] Add `prompt()` to `AssessmentBackend` protocol in `assessment.py`

## Backend Implementations

- [x] Add `_parse_prompt_response()` helper to `assessment.py` for building prompt response dicts with optional JSON parsing
- [x] Implement `AnthropicApiBackend.prompt()` delegating to `ClaudeService.prompt_text()`
- [x] Implement `ClaudeCliBackend.prompt()` invoking `claude --print <text>` subprocess
- [x] Implement `OpenCodeBackend.prompt()` invoking `opencode run <text>` subprocess
- [x] Implement `NullBackend.prompt()` returning `{"text": "", "data": None}`
- [x] Add `ClaudeService.prompt_text()` method with retry/circuit-breaker wrappers and JSON-mode system instruction

## Empty Prompt Guard

- [x] All backend `prompt()` methods return `{"text": "", "data": None}` immediately for empty/whitespace-only prompts

## JSON Response Format

- [x] When `response_format="json"`, parse model response as JSON into `data` field
- [x] Handle invalid JSON gracefully: `data=None` + warning log
- [x] Handle markdown code-fenced JSON responses

## Refactor 029 AI Classification

- [x] Refactor `_classify_with_ai()` in `classify_human_feedback.py` to use `prompt(response_format="json")` instead of `assess()` with a synthetic card
- [x] Remove synthetic card hack
- [x] Preserve fallback to text parsing when `data` is None

## Tests

- [x] Add `TestParsePromptResponse` tests for `_parse_prompt_response()` helper
- [x] Add `TestNullBackendPrompt` tests
- [x] Add `TestAnthropicApiBackendPrompt` tests (delegates to `prompt_text`, empty prompt guard, JSON format, error propagation)
- [x] Add `TestClaudeCliBackendPrompt` tests (subprocess call, JSON format, empty prompt, timeout, OSError, CLI args)
- [x] Add `TestOpenCodeBackendPrompt` tests (subprocess call, JSON format, empty prompt, CLI args, timeout)
- [x] Update `TestAIClassification` tests to mock `prompt()` instead of `assess()`
- [x] Add `test_ai_data_none_text_parseable` test for fallback text parsing in `_classify_with_ai()`

## Validation

- [x] All existing tests pass (1379 passed)
- [x] Lint clean (`ruff check src/ tests/`)
