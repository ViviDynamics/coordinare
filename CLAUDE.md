See AGENTS.md for all development guidelines.

## Active Technologies
- Python 3.14 (project minimum: 3.12; production currently on 3.14.5 via uv) + pydantic 2.x, langgraph, structlog, docker SDK (or subprocess to `docker` CLI), fastapi (dashboard SSE), httpx (performer HTTP transport), pyyaml (config) (076-qa-cycle)
- JSON snapshot on local disk via `state_store.py` (single-host single-process) (076-qa-cycle)
- Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv) + pydantic 2.x (config + schemas), the performer backend layer (`agent/performer/src/performer/backends/`), httpx (HTTP performer transport), docker CLI (ephemeral performers), the LiteLLM proxy (OpenAI-compatible model gateway) (077-multi-backend-qa)
- N/A for this round (no new persisted state; reuses 076's snapshot/state_store unchanged) (077-multi-backend-qa)
- Python 3.14 (project minimum 3.12; prod 3.14.5 via uv) + pydantic 2.x (config models + validation), aiohttp (the existing `ClaudeCodeShim` reverse-proxy server; reused for `DualModelProxy`), httpx (upstream client calls), structlog (observability). No new external dependencies anticipated. (080-dual-model-orchestration)
- N/A for coordinare state. Orchestration observability (FR-021) written to the job's existing durable artifact/capture dir (same `capture_dir` mechanism `ClaudeCodeShim` already accepts) — not a new store. (080-dual-model-orchestration)

## Recent Changes
- 076-qa-cycle: Added Python 3.14 (project minimum: 3.12; production currently on 3.14.5 via uv) + pydantic 2.x, langgraph, structlog, docker SDK (or subprocess to `docker` CLI), fastapi (dashboard SSE), httpx (performer HTTP transport), pyyaml (config)
