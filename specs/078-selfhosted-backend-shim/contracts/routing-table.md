# Contract: Routing Table Config Surface

Defines the operator-facing config schema for the `(backend, model)` → target
routing table (FR-078-4). Decoupled from per-backend config. Validated at load
(pydantic 2.x) — invalid entries fail fast, not at runtime.

## Shape

```yaml
selfhosted_routing:
  - backend: openclaw
    model: gpt-oss:120b
    target:
      base_url: http://spark:11434/v1   # Ollama-direct, clean upstream
      wire_format: openai
      strategy: reroute                 # bypass lossy LiteLLM entirely
      reroute_upstream: null            # already the clean path
  - backend: codex
    model: gpt-oss:120b
    target:
      base_url: http://litellm:4000/v1
      wire_format: openai
      strategy: normalize
      normalizers: [harmony_tool_calls] # explicit; only these run
      reroute_upstream: http://spark:11434/v1   # auto-reroute on probe failure
  - backend: claude_code
    model: qwen3-coder
    target:
      base_url: http://litellm:4000
      wire_format: anthropic
      strategy: normalize
      normalizers: [strip_reasoning]
      reroute_upstream: null
```

## Resolution rules

- Key is `(backend, model)`; backend id normalized (kebab→snake).
- **No matching entry → native-cloud no-op** (proxy not launched, bytes unchanged).
  (FR-078-1, SC-003)
- A target explicitly marked native is a no-op even if a `base_url` is present.
  (Edge Case)

## Validation rules

- `strategy: reroute` ⇒ `normalizers` MUST be empty/absent. (SC-004)
- `strategy: normalize` ⇒ `normalizers` non-empty; every key MUST exist in
  `NORMALIZER_REGISTRY`. (FR-078-2)
- Unknown `normalizers` key, missing `base_url`, or invalid `wire_format` →
  load-time validation error.
- `reroute_upstream`, when set, is the auto-reroute fallback used by health
  gating on probe failure. (FR-078-5)
- A backend with no entry in `PROVIDER_BASE_URL_ENV` (e.g. hermes) appearing in a
  routing entry → clear "cannot route" error. (FR-078-4, Edge Case)

## Field Registry (for /speckit.analyze contract check)

This is a **performer-side config surface**, not a dispatch-payload field. No new
field crosses the coordinare → performer dispatch boundary.

| Field | Carrier | Notes |
|---|---|---|
| `selfhosted_routing` | performer config (in-container) | routing table; not on dispatch payload |
| `backend` / `model` | `RoutingEntry` | lookup key |
| `target.base_url` / `wire_format` / `strategy` / `normalizers` / `reroute_upstream` | `TargetDescriptor` | resolved target |
