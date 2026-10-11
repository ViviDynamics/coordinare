# MCP management layer

Epic. Lets an external AI agent (Claude Code, OpenCode, or any MCP client) help configure and manage a coordinare deployment through a Model Context Protocol (MCP) server. The server proxies the existing dashboard REST API, which is already the de-facto management contract: the dashboard front end is itself a REST client.

Phase 1 (this spec) ships the remote streamable HTTP transport with bearer auth and the full operator tool surface. Phase 2 (`specs/552-mcp-stdio-local/`) adds a stdio transport for same-host agents. Phase 3 (`specs/553-mcp-oauth21/`) upgrades auth to the MCP-spec OAuth 2.1 resource-server model against the existing OIDC provider.

## Architecture

- New module `src/coordinare/mcp/` with entry point `python -m coordinare.mcp`. Internal units: `server.py` (protocol wiring, tool registry, transport selection), `tools/` (typed async tool functions grouped by domain), `client.py` (thin httpx REST client of the dashboard API), `auth.py` (bearer check now, OAuth resource server later).
- MCP SDK: official `mcp` Python SDK (modelcontextprotocol/python-sdk). Streamable HTTP and stdio transports, pydantic v2 based.
- Stateless by design: all state lives in the daemon, reached through the existing REST contract. No new persistence, no cache, per-call connections.
- Deployment: sidecar container in the existing Helm statefulset pod, reaching the dashboard over `localhost:8090`. Existing ClusterIP Service gains an MCP port; the off-by-default Ingress routes `/mcp` to it.

## User stories and acceptance

### US1 — Agent connects remotely (P1)
An operator points a coding agent at `https://<host>/mcp` with a bearer token. The agent lists tools and receives the full operator surface. Unauthenticated or bad-token requests are refused with a structured 401 the client can surface.

### US2 — Agent manages config (P1)
The agent reads redacted config, prepares a change, validates it without writing, and writes it with optimistic concurrency. A write without the `base_hash` from the most recent read is refused with a conflict error that names `get_config` as the recovery step. Write results carry `applied: hot_reloaded | staged_restart | staged_next_job` so the agent can tell the operator when a restart is required.

### US3 — Agent inspects live state (P1)
The agent reads the dashboard snapshot, performer logs (buffered tail), activity feed, and Prometheus metrics. Read-only tools carry `readOnlyHint: true` so clients auto-approve them.

### US4 — Agent drives lifecycle controls (P1)
The agent invokes `control` with an action enum (`force_poll`, `cancel`, `skip_role`, `restart_from`, `veto`, `dry_run`) and the required card/role arguments. Every lifecycle call is destructive-flagged and audit-logged.

### US5 — Agent manages symphonies and personas (P1)
The agent lists, reads, upserts, validates, and deletes symphonies, and reads, upserts, and deletes personas, mirroring the dashboard REST semantics. Delete tools carry `destructiveHint: true`.

### US6 — Audit trail (P1)
Every mutating tool call emits one structured-log line (`mcp.tool_invoked`) with tool name, action, and client credential fingerprint, using the repo's `snake_case.dot.separated` structlog vocabulary.

## Functional requirements

- FR1: MCP server exposes 16 consolidated tools in five domains (config, symphonies, personas, lifecycle, state inspection), each mapped to existing dashboard REST endpoints. Every REST verb is accounted for in the tool registry or listed in an explicit, asserted defer-list.
- FR2: Tool annotations per MCP spec: `readOnlyHint`, `destructiveHint`, `idempotentHint`.
- FR3: Auth phase 1: incoming MCP requests present `COORDINARE_MCP_TOKEN` as a bearer header, constant-time compared. This is a distinct secret from the dashboard operator token; the server holds the dashboard token internally (`COORDINARE_MCP_DASHBOARD_TOKEN`) and never discloses it. Bearer mode remains a fallback in phase 3 behind a config flag.
- FR4: Error mapping consistent across tools: 401/403 as structured auth failure; 409 verbatim with recovery hint; 400/422 with field-level detail; 5xx/timeout as "daemon may be mid-cycle" with a read-only-probe-first suggestion.
- FR5: Timeouts default 10 s for reads, 60 s for lifecycle controls, overridable in config. No SSE streaming proxying in phase 1.
- FR6: Audit logging per US6, token fingerprint in phase 1, `sub` claim in phase 3.
- FR7: Helm chart adds the sidecar container, Service port, and Ingress `/mcp` routing, template-tested.
- FR8: Tool surface consolidates rather than mirrors: one tool per domain verb group with `action` enums, never 40 one-to-one tools.
- FR9: Dashboard OIDC login flow is untouched; MCP has its own credential boundary in all phases.

## Testing

- Contract tests: every tool driven against a FastAPI `TestClient` of the real dashboard app, so REST drift breaks MCP tests in the same commit.
- Unit tests: tool registry completeness against the asserted defer-list, bearer auth behavior, timeout config, error mapping table.
- Transport smoke test: in-process `mcp` client over the phase-1 transport (streamable HTTP) driving representative tools end to end.
- Chart tests: sidecar, Service port, and Ingress `/mcp` rendering under test values.
- Gate through existing `make test` / `make lint` / `make typecheck`.

## Scope and assumptions

- The dashboard REST API is the stable seam; the MCP server proxies it as a client. Three small additive routes join the dashboard where JSON reads are missing (`GET /api/state` for the snapshot, `GET /api/activity` for the feed) or the preflight is only a CLI verb (`POST /api/config/validate`, write-free); every other tool maps to an existing route.
- The MCP server accepts the full operator authority model (all-or-nothing) for phase 1, matching the existing threat model. Per-identity authorization arrives with phase 3 OAuth and is out of scope here.
- No persistence on the MCP side; nothing survives a sidecar restart.
- Phase 2 and phase 3 are separate specs; this spec delivers phase 1 only.

## Success criteria

- SC1: Claude Code configured with the endpoint and token lists all tools and completes an inspect-validate-update-reload config round trip.
- SC2: A stale-`base_hash` write is refused with a conflict result naming the recovery path.
- SC3: Read-only tools auto-approve in the client; mutating tools require explicit approval.
- SC4: A daemon restart surfaces to the agent as "daemon unreachable" error results, and the MCP server reconnects per call without manual intervention.
- SC5: All contract, unit, transport, and chart tests pass under `make test`; lint and typecheck clean.
