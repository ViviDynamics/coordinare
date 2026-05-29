See AGENTS.md for all development guidelines.

## Active Technologies
- Python 3.14 (project minimum: 3.12; production currently on 3.14.5 via uv) + pydantic 2.x, langgraph, structlog, docker SDK (or subprocess to `docker` CLI), fastapi (dashboard SSE), httpx (performer HTTP transport), pyyaml (config) (076-qa-cycle)
- JSON snapshot on local disk via `state_store.py` (single-host single-process) (076-qa-cycle)

## Recent Changes
- 076-qa-cycle: Added Python 3.14 (project minimum: 3.12; production currently on 3.14.5 via uv) + pydantic 2.x, langgraph, structlog, docker SDK (or subprocess to `docker` CLI), fastapi (dashboard SSE), httpx (performer HTTP transport), pyyaml (config)
