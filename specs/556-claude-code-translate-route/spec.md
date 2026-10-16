# 556: Claude Code translate route for issue 223

The requested transport already exists in spec 084. Expose its LiteLLM opt-in in
the main config example and configuration reference, and pin the issue's exact
stream transition with regression tests. Do not add another activation key.

## Requirements

1. `SELFHOSTED_ROUTING_CONFIG` remains empty by default. A matching
   `selfhosted_routing` entry with `target.strategy: translate` and
   `wire_format: openai` selects the performer translator. Unmatched pairs and
   existing Anthropic routes keep their behavior.
2. Reasoning-only deltas open no text block. A mixed delta containing
   `reasoning: ' no fences.'` and `content: '{"'` retains that content, followed
   by the remaining JSON, with or without `strip_reasoning`.
3. The launcher sends Claude Code to the loopback shim, suppresses its older
   LiteLLM shim, and restores environment variables on teardown. Requests go to
   `/v1/chat/completions` using the dispatched model and gateway credentials.
4. Document the garden ConfigMap and performer mount requirements. No live
   deployment or GitHub operation is in scope.

## Acceptance

Run daemon unit regressions under `tests/unit/`, relevant performer routing and
translation tests, lint, mypy, config validation and the existing spec-084
translator benchmark (median <= 5 ms, p99 <= 15 ms). Full unit tests if feasible.
No new runtime dependency, network hop, cache, payload field or persistence.
