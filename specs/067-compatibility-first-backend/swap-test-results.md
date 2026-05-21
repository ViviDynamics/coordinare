# 067 Swap-Test Results (SC-003 / FR-006)

Hand-run of `quickstart.md` (Steps 4-5 + the Swap test) against each portable
endpoint. One section per endpoint; record model id, `n_ctx`, and pass/fail
per quickstart step. This is the US3 end-to-end verification artifact and
satisfies FR-006's non-CI portability claim.

> **Status**: Operator-driven. Each section below is a template to fill in
> during the live run; a section that lists `pass` for every step closes that
> endpoint's portability claim.

## LM Studio

- Endpoint: `http://localhost:1234/v1`
- Model id: _e.g._ `qwen3-coder-30b`
- `n_ctx`: _e.g._ `32768`

| Step | Result | Notes |
|------|--------|-------|
| 1. Config loaded with `backend: opencode_compat` |  |  |
| 2. Card dispatched |  |  |
| 3. Reached `IN_REVIEW` |  |  |
| 4. No forbidden warnings in LM Studio console |  |  |
| 5. SC-003 swap from `codex` → `opencode_compat` (string-only config flip) |  |  |

## vLLM

- Endpoint: _e.g._ `http://vllm.local:8000/v1`
- Model id: 
- `n_ctx`: 

| Step | Result | Notes |
|------|--------|-------|
| 1. Config loaded with `backend: opencode_compat` |  |  |
| 2. Card dispatched |  |  |
| 3. Reached `IN_REVIEW` |  |  |
| 4. No transient classification noise |  |  |
| 5. SC-003 swap from `codex` → `opencode_compat` |  |  |

## Ollama

- Endpoint: _e.g._ `http://localhost:11434/v1`
- Model id: 
- `n_ctx`: 

| Step | Result | Notes |
|------|--------|-------|
| 1. Config loaded with `backend: opencode_compat` |  |  |
| 2. Card dispatched |  |  |
| 3. Reached `IN_REVIEW` |  |  |
| 4. Tool-call shape accepted by ollama's OpenAI shim |  |  |
| 5. SC-003 swap from `codex` → `opencode_compat` |  |  |

## LiteLLM Proxy

- Endpoint: _e.g._ `http://litellm.local:4000/v1`
- Upstream model id (proxied): 
- `n_ctx`: 

| Step | Result | Notes |
|------|--------|-------|
| 1. Config loaded with `backend: opencode_compat` |  |  |
| 2. Card dispatched |  |  |
| 3. Reached `IN_REVIEW` |  |  |
| 4. Transient 5xx body markers honored (rate-limit/temporarily unavailable) |  |  |
| 5. SC-003 swap from `codex` → `opencode_compat` |  |  |
