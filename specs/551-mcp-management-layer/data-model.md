# Phase 1 Data Model: MCP management layer

Feature: `551-mcp-management-layer` | Branch: `551-mcp-management-layer` | Date: 2026-10-11

The MCP server owns no persistent state. Every entity below is either configuration loaded at boot, an in-memory value object created per tool call, or a log line. This is a deliberate design property (spec SC4): the sidecar can restart freely.

## Entity: McpServerConfig

Boot-time settings, loaded from environment variables with the same `${VAR}` expansion discipline as `CoordinareConfiguration` (`config.py`). CLI flags override env, env overrides defaults, mirroring pydantic-settings priority in `__main__.py`.

| Field | Type | Default | Validation |
|---|---|---|---|
| `transport` | `Literal["http"]` | `"http"` | stdio arrives in phase 2 |
| `listen_host` | str | `"0.0.0.0"` | sidecar must accept pod-external traffic from the Service |
| `listen_port` | int | `8091` | 1..65535; must differ from 8080/8090 |
| `agent_token` | `SecretStr` | required | min length 32, constant-time compared |
| `dashboard_url` | str | `"http://localhost:8090"` | must parse as URL; loopback default fits the sidecar |
| `health_url` | str | `"http://localhost:8080"` | health server (owns `/metrics`); loopback default fits the sidecar |
| `dashboard_token` | `SecretStr` | required | the existing dashboard operator token |
| `read_timeout_seconds` | float | `10.0` | > 0, < 300 |
| `lifecycle_timeout_seconds` | float | `60.0` | > 0, < 600 |
| `server_name` | str | `"coordinare"` | presented in MCP initialize |

Relationships: consumed by `server.py` (transport + protocol wiring), `auth.py` (token check), `client.py` (URL, timeouts, dashboard token).

## Entity: ToolDefinition

Static registration record describing one MCP tool. Built at import time by the `tools/` package; the registry is immutable at runtime.

| Field | Type | Notes |
|---|---|---|
| `name` | str | snake_case tool name, e.g. `update_config` |
| `domain` | `Literal["config", "symphonies", "personas", "lifecycle", "state"]` | matches the five REST surface groups |
| `description` | str | one or two sentences; agents read these |
| `input_model` | pydantic model | validated by the SDK before the handler runs |
| `annotations` | MCP ToolAnnotations | `readOnlyHint`, `destructiveHint`, `idempotentHint` |
| `rest_calls` | tuple of (method, path pattern) | used by the contract test to verify coverage and by the completeness test against the defer-list |
| `handler` | async callable | takes parsed input + `DashboardClient`, returns `ToolResult` |

Invariants:
- Every mutating tool sets `readOnlyHint: false` (implicit) and destructive tools set `destructiveHint: true`.
- Read-only tools set `readOnlyHint: true` and never issue non-GET/HEAD REST calls (asserted by contract test).
- The registry covers every dashboard REST verb group except the explicit defer-list (`/oidc/*`, dashboard pages, static assets, SSE `/events`, webhook path). The defer-list is asserted, so a new undocumented REST verb fails the completeness test.

## Entity: DashboardClient

Per-call async httpx wrapper (R6). Not shared state; constructed per invocation or per session, holding base URL, dashboard token, and the timeout budget for the tool's domain.

| Field | Type | Notes |
|---|---|---|
| `base_url` | str | from `McpServerConfig` |
| `_dashboard_token` | `SecretStr` | sent as `Authorization: Bearer ...` on the internal hop; never rendered or logged |
| `read_timeout` / `lifecycle_timeout` | float | chosen by the calling tool |

Behavior: `get(method, path, ...)`, `send(method, path, ...)`; every response is converted to a `ToolResult` via the error-mapping table; no retries, no connection pooling.

## Entity: ToolResult

The uniform return type for every tool handler (R5/R8: consistent error mapping across tools).

| Field | Type | Notes |
|---|---|---|
| `ok` | bool | |
| `data` | `dict[str, Any] \| None` | parsed JSON body on success; redacted already by the dashboard |
| `error` | `ToolError \| None` | mutually exclusive with `data` |
| `error.kind` | `Literal["auth_failed", "conflict", "validation", "daemon_unreachable", "daemon_error"]` | stable vocabulary for agents |
| `error.status` | int | HTTP status of the REST call (0 for transport timeout) |
| `error.detail` | str | field-level validation detail or conflict body verbatim |
| `error.recovery_hint` | str | e.g. "re-read via get_config and retry with the fresh base_hash" |

## Entity: AuditRecord

Emitted as one structlog line, never persisted.

| Field | Type | Notes |
|---|---|---|
| `event` | `Literal["mcp.tool_invoked"]` | constant |
| `tool` | str | |
| `action` | str \| None | action-enum value when the tool has one |
| `outcome` | `Literal["ok", "error"]` | |
| `error_kind` | str \| None | |
| `credential_fingerprint` | str | first 8 hex of sha256 of the agent token; never the token |
| `duration_ms` | int | |

## Entity: ErrorMapping

Static table (research R4/FR4), applied in `client.py`:

| REST response | ToolResult |
|---|---|
| 401, 403 | `auth_failed` |
| 409 | `conflict`, body verbatim, recovery hint names the re-reading tool |
| 400, 422 | `validation`, field detail verbatim |
| 5xx | `daemon_error`, truncated body |
| transport timeout / connection error | `daemon_unreachable`, status 0 |

## No state transitions

Unlike most coordinare entities, none of these have lifecycles or persistence. The daemon's own state machines (phases, sessions, wedges) remain entirely on the daemon side of the REST boundary.
