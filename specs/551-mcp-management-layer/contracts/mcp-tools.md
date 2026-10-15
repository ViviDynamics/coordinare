# Phase 1 Contract: MCP tool surface

Feature: `551-mcp-management-layer` | Branch: `551-mcp-management-layer` | Date: 2026-10-11

The authoritative tool-to-REST mapping for phase 1. 16 tools, five domains. "Additive" marks the only daemon-side changes: two new read-only GET routes and one validation route, all under the existing dashboard auth middleware. Contract tests in `tests/unit/test_551_mcp_contract.py` enforce this table.

Common input parameter (all tools): none required by MCP itself; the agent authenticates at the MCP endpoint with `Authorization: Bearer <COORDINARE_MCP_TOKEN>` (constant-time checked by `auth.py`). Every tool result is a `ToolResult` per `data-model.md`; error `kind` values come from the shared error-mapping table.

## Domain: config

### get_config
Reads redacted configuration. Read-only, idempotent.

| Param | Type | Req | Notes |
|---|---|---|---|
| `scope` | `enum: all, effective, global, section, catalog, routing` | yes | which read to perform |
| `id` | str | only for `section`/`catalog` | section id or catalog name |

REST mapping: `all`→GET `/api/config/all`, `effective`→GET `/api/config/effective`, `global`→GET `/api/config/global`, `section`→GET `/api/config/section/{id}`, `catalog`→GET `/api/config/catalog/{id}`, `routing`→GET `/api/config/routing`.

Result includes `base_hash` when the scope supports optimistic concurrency (`global`, `section`, catalog items). The routing scope returns the same value as `content_hash`; pass it as `base_hash`. Each routing entry carries its `backend` and `model`, which `update_config` takes as `expected`. Secrets are redacted by the dashboard already; the MCP layer must not and cannot un-redact them.

### update_config
Writes configuration with optimistic concurrency. Destructive.

| Param | Type | Req | Notes |
|---|---|---|---|
| `action` | `enum: put_global, put_section, catalog_create, catalog_update, catalog_delete, routing_create, routing_update, routing_delete, reload` | yes | |
| `id` | str | per action | section id, catalog name, catalog item id, or routing entry index |
| `payload` | object | per action | validated by the dashboard's pydantic models |
| `base_hash` | str | required for put/put/update/delete | must come from the latest `get_config` |
| `expected` | object `{backend, model}`, exactly those two keys | no; routing_update and routing_delete only | 225: the routing entry the write means. Any other key (a whole entry with `target`) is a 400 naming the keys to remove. The dashboard refuses (409) when the entry at `index` is another one. Take `backend` and `model` from the same `get_config` routing read as `index` and `base_hash`. Omitting it keeps the `base_hash` guard alone. |
| `bootstrap` | `enum: env, wiki` | no | symphony-domain chaining only; see `upsert_symphony` |

REST mapping: `put_global`→PUT `/api/config/global`; `put_section`→PUT `/api/config/section/{id}`; `catalog_create`→POST `/api/config/catalog/{id}`; `catalog_update`→PUT `/api/config/catalog/{id}/{item_id}`; `catalog_delete`→DELETE `/api/config/catalog/{id}/{item_id}`; `routing_create`→POST `/api/config/routing/entry`; `routing_update`→PUT `/api/config/routing/entry/{index}`; `routing_delete`→DELETE `/api/config/routing/entry/{index}`; `reload`→POST `/api/config/reload`.

Result: `applied: hot_reloaded | staged_restart | staged_next_job` plus the written section. A 409 maps to `conflict` with a recovery hint to re-read via `get_config`.

### validate_config (additive)
Validates a config payload without writing. Read-only. **Additive**: POST `/api/config/validate` on the dashboard, delegating to the existing `config_validation.validate_config` preflight used by `python -m coordinare config validate` and boot. No write, no reload.

| Param | Type | Req | Notes |
|---|---|---|---|
| `payload` | object | yes | full or partial config to validate |

## Domain: symphonies

### list_symphonies
Read-only, idempotent. REST: GET `/api/symphonies`.

### get_symphony
Read-only, idempotent. Param: `name` (str). REST: GET `/api/symphonies/{name}`.

### upsert_symphony
Creates or replaces one symphony. Destructive. Param: `name` (str), `spec` (object), optional `bootstrap` (`enum: env, wiki`; when present the server chains POST `/api/symphonies/{name}/env-bootstrap` or `/wiki-init` after the write). REST: PUT `/api/symphonies/{name}` (POST `/api/symphonies` for create), chained bootstrap per above.

### delete_symphony
Deletes one symphony. Destructive. Param: `name`. REST: DELETE `/api/symphonies/{name}`.

### validate_symphony
Validates a symphony payload without writing. Read-only. Param: `name`, `spec`. REST: POST `/api/symphonies/{name}/validate`.

## Domain: personas

### get_personas
Read-only, idempotent. REST: GET `/api/personas`.

### upsert_persona
Creates or replaces one role persona. Destructive. Param: `role` (str), `body` (object). REST: PUT `/api/personas/{role}`.

### delete_persona
Deletes one role persona. Destructive. Param: `role`. REST: DELETE `/api/personas/{role}`.

## Domain: lifecycle

### control
Single entry point for lifecycle overrides. Destructive, not idempotent (actions have one-shot semantics).

| Param | Type | Req |
|---|---|---|
| `action` | `enum: force_poll, cancel, skip_role, restart_from, veto, dry_run` | yes |
| `card_id` | str | required by `cancel`, `veto`, `dry_run` |
| `role` | str | required by `restart_from`; optional for `skip_role` |
| `role_index` | int | optional for `restart_from` |
| `reason` | str | no |

REST mapping: `force_poll`→POST `/api/force-poll`; `cancel`→POST `/api/cancel`; `skip_role`→POST `/api/skip-role`; `restart_from`→POST `/api/restart-from/{role}`; `veto`→POST `/api/veto`; `dry_run`→POST `/api/dry-run/{card_id}`.

Uses the 60 s lifecycle timeout budget. 202/409 statuses from force-poll map to `ok`/`conflict` respectively, per the dashboard's own contract.

## Domain: state inspection

All read-only and idempotent.

### get_state — **Additive**: GET `/api/state`
JSON snapshot mirroring the `state_update` SSE payload (the dashboard today exposes it only over SSE and HTML). Additive read-only route on the dashboard app, same auth middleware.

### get_performer_logs
Param: session/card filters per the existing route's query parameters. REST: GET `/api/performer-logs`. Buffered tail; no SSE proxying.

### get_activity — **Additive**: GET `/api/activity`
JSON read of the in-memory `ActivityLog` (newest-first, same entries the SSE backfill delivers), optional `card_id` filter. Additive read-only route.

### get_metrics
Prometheus exposition format, passed through verbatim. REST: GET `{health_url}/metrics` (health server, not the dashboard). No parsing in phase 1; the agent reads it.

## Defer-list (asserted)

REST surfaces deliberately NOT proxied in phase 1: `/oidc/*` (browser flow), `/static/*` and HTML pages (human UI), `/events` (SSE), `/api/assistant/*` (config-assistant browser helper), signed webhook path, `/health`, `/live`, `/ready`. The registry completeness test asserts both directions: every tool's REST calls exist, and every dashboard route not in this list is covered by a tool.

## Auth contract (phase 1)

- MCP endpoint auth: `Authorization: Bearer <COORDINARE_MCP_TOKEN>`, constant-time compare, min length 32.
- Internal hop: `Authorization: Bearer <COORDINARE_MCP_DASHBOARD_TOKEN>` added by `DashboardClient`; never echoed into results or logs.
- Mutating MCP calls additionally require same-origin semantics identical to the dashboard's (the MCP protocol's POST carries no browser Origin, so the Origin guard does not apply; the bearer check is the boundary).
