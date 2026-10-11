# Implementation Plan: MCP management layer

**Branch**: `551-mcp-management-layer` | **Date**: 2026-10-11 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/551-mcp-management-layer/spec.md`

## Summary

Ship a standalone MCP server (`python -m coordinare.mcp`) that proxies the existing dashboard REST API, giving an external AI agent (Claude Code, OpenCode, any MCP client) the full operator surface: config read/validate/update with optimistic concurrency, symphony CRUD, personas, lifecycle controls, and live state inspection. Streamable HTTP transport, bearer auth with a dedicated agent token, Helm sidecar deployment, structured audit logging.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `mcp` (official modelcontextprotocol SDK, new dependency), httpx (existing), pydantic v2 (existing), structlog (existing)
**Storage**: N/A. The MCP server is stateless; all state lives in the daemon behind the dashboard REST contract
**Testing**: pytest + pytest-asyncio; contract tests run tools against a FastAPI TestClient of the real dashboard app
**Target Platform**: Linux container (Helm sidecar) on the daemon's statefulset pod; macOS/Linux dev workstations
**Project Type**: single
**Performance Goals**: tool call p95 < 2 s for state/config reads (network-bound, no local compute); server cold start < 5 s
**Constraints**: 10 s default read timeout, 60 s lifecycle-control timeout; no pooled connections to the daemon; sidecar restarts are always safe
**Scale/Scope**: one daemon (single replica), one to a handful of concurrent agent sessions; 16 tools in 5 domains

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | New module follows repo conventions (`from __future__ import annotations`, pydantic v2 models, structlog events, type-annotated public interfaces). One justified new dependency (`mcp` SDK). |
| II. Testing Discipline | PASS | Contract tests (tools vs real dashboard app via TestClient), unit tests (registry completeness, auth, error mapping, timeouts), transport smoke test, chart tests. Deterministic; no live network. Coverage must not regress (433 ratchet). |
| III. User Experience Consistency | N/A (no UI) | Agent-facing UX is tool descriptions and error results; consistency requirement applies to error mapping table (uniform across all tools) and MCP tool annotations. |
| IV. Performance by Design | PASS | Budgets: 10 s read / 60 s lifecycle timeouts (spec FR5), p95 < 2 s for reads, cold start < 5 s; measured by tasks.md T040 during the T039 quickstart run. No caching, so no invalidation strategy to document. Stateless by design. |
| V. Clarity Before Action | PASS | Ambiguities resolved in brainstorming (client type, transport, auth phasing, scope). Resolutions recorded in research.md. No `NEEDS CLARIFICATION` remains. |

## Project Structure

### Documentation (this feature)

```text
specs/551-mcp-management-layer/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── mcp/
│   ├── __init__.py
│   ├── __main__.py      # python -m coordinare.mcp entry point
│   ├── server.py        # MCP server assembly, tool registry, transport selection
│   ├── config.py        # MCP server settings (env-expanded, like CoordinareConfiguration)
│   ├── auth.py          # bearer-token check (phase 1); OAuth resource server (phase 3)
│   ├── client.py        # thin async httpx REST client of the dashboard API
│   ├── audit.py         # mcp.tool_invoked structured-log emitter
│   └── tools/
│       ├── __init__.py  # tool registry: domain -> tools, annotations
│       ├── config_tools.py
│       ├── symphony_tools.py
│       ├── persona_tools.py
│       ├── lifecycle_tools.py
│       └── state_tools.py

tests/unit/
├── test_551_mcp_tool_registry.py
├── test_551_mcp_auth.py
├── test_551_mcp_error_mapping.py
├── test_551_mcp_contract.py        # tools vs real dashboard app (TestClient)
└── test_551_mcp_transport_smoke.py

deploy/helm/coordinare/
├── values.yaml              # sidecar image/port, mcp token secret
├── templates/statefulset.yaml   # sidecar container
├── templates/service.yaml       # new MCP port
└── templates/ingress.yaml       # /mcp route
```

**Structure Decision**: Single-project layout. The MCP server is a new package under `src/coordinare/mcp/` because it shares the repo's config-expansion conventions, structlog setup, and is packaged/shipped with the daemon image. Tools are split by domain, one file per REST surface they proxy, so the registry stays reviewable file by file.

## Complexity Tracking

No constitution violations to justify. Notable deliberate decisions (recorded in research.md, not violations):

| Decision | Why | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Proxy the REST API rather than call daemon services directly | Keeps the daemon lean; REST surface is already the tested management contract | In-process embedding couples MCP's crash domain to the daemon and blocks the phase-2 stdio transport |
| Separate agent token from dashboard token | Limits blast radius of a leaked agent credential; independent rotation | Reusing the dashboard token for both hops saves one secret but makes a leak a full breach |
| Tool consolidation (16 tools with action enums) | Agents perform better with fewer, richer tools; annotations carry safety hints | One tool per REST endpoint (40+) overloads tool selection; heavy merging hides semantics |
