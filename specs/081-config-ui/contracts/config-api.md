# Contract: Config UI API (`/api/config/*`)

**Feature**: 081-config-ui | **Date**: 2026-06-06 | **Plan**: [../plan.md](../plan.md)

These endpoints **extend** the existing dashboard surface in `src/coordinare/dashboard.py`.
The existing endpoints are **preserved unchanged**:
- `GET /api/config/effective` (read-only effective config)
- `GET|PUT /api/config/global` (11-field global slice)
- `GET|PUT|DELETE /api/personas/{role}`
- `POST /api/config/reload` (hot-reload trigger)

New/extended endpoints below. All responses are JSON. Errors never expose raw stack
traces or pydantic dumps — `errors[].message` is operator-readable (UX Principle III).

---

## Field Registry

> The 078 routing config is written to a host-side YAML mounted into performers; it is
> **not** added to the coordinare→performer dispatch payload. No dispatch-payload fields
> are added or changed by this feature. Registry below is the API transfer surface.

| Field | Type | Endpoint(s) | Notes |
|-------|------|-------------|-------|
| `sections` | list | GET /api/config/all | Full editable surface (E2) |
| `content_hashes` | object | GET /api/config/all | Optimistic-concurrency baselines (D4) |
| `routing_available` | bool | GET /api/config/all, GET /api/config/routing | False when no endpoint mounts routing YAML |
| `store` | enum | save requests | `config_yaml` \| `routing_yaml` |
| `section` | str | save requests | Section id |
| `changes` | object | save requests | Edited keys only |
| `base_hash` | str | save requests | Required precondition |
| `applied` | enum | save responses | `hot_reloaded` \| `staged_restart` \| `staged_next_job` |
| `referenced_by` | list | catalog reads | Delete-protection metadata (D6) |
| `secret` | bool | descriptors | Masked-value flag (D3) |
| `is_env_placeholder` | bool | descriptors | Raw `${VAR}` literal flag (D3) |

---

## GET /api/config/all

Return the full editable configuration surface as descriptors, grouped into sections,
with content-hash baselines.

**Response 200** — `ConfigSnapshot` (data-model E5):
```json
{
  "sections": [ { "id": "global", "title": "Global Tuning", "kind": "scalar_group",
                  "store": "config_yaml", "settings": [ /* ConfigSetting */ ] }, ... ],
  "content_hashes": { "config_yaml": "sha256:…", "routing_yaml": "sha256:…" },
  "config_version": 7,
  "routing_available": true
}
```
Secrets are masked; `${VAR}` literals returned raw. **Performance**: < 300 ms p95 (SC-010).

---

## GET /api/config/section/{section_id}

Return one section's descriptors (lighter payload for focused editing).
**404** if `section_id` unknown. Same masking rules as `/all`.

---

## PUT /api/config/section/{section_id}

Save edits to a `scalar_group` section (e.g. `global`, a single symphony's overrides).

**Request** — `SaveRequest` (data-model E6):
```json
{ "store": "config_yaml", "section": "global",
  "changes": { "poll_interval_seconds": 10, "log_level": "DEBUG" },
  "base_hash": "sha256:…" }
```

**Response 200** — `SaveResult`:
```json
{ "ok": true, "applied": "hot_reloaded", "new_hash": "sha256:…", "errors": [] }
```
**Response 409** (concurrency) / **422** (validation):
```json
{ "ok": false, "applied": null, "new_hash": null,
  "errors": [ { "key": "poll_interval_seconds", "code": "validation",
               "message": "Must be between 1 and 3600 seconds." } ] }
```

---

## Catalog CRUD (spec-080) — `endpoints` | `model_endpoints` | `modes`

`{catalog}` ∈ `endpoints`, `model_endpoints`, `modes`. All write through
`config_write_service` with full pydantic + cross-catalog validation (D6) and
optimistic concurrency (D4).

- **GET /api/config/catalog/{catalog}** → `{ items: [CatalogItem], content_hash }`
  (each item carries `referenced_by` / `deletable`).
- **POST /api/config/catalog/{catalog}** — create. Body: `{ item, base_hash }`.
  `422` on validation (e.g. `model_endpoint.endpoint` names an unknown endpoint).
- **PUT /api/config/catalog/{catalog}/{id}** — update. Body: `{ changes, base_hash }`.
- **DELETE /api/config/catalog/{catalog}/{id}** — delete. Body: `{ base_hash }`.
  `409` with `code:"referenced"` and a message naming the referrer when still referenced
  (delete-protection, D6).

All catalog writes are `applied: "hot_reloaded"` when the daemon reload path swaps them
live, else `staged_restart`.

---

## Routing table CRUD (spec-078) — `/api/config/routing`

Backed by the host-side YAML mounted into performers (research D2). Validated by the
spec-078 models (`RoutingTable`/`RoutingEntry`/`TargetDescriptor`).

- **GET /api/config/routing** →
  `{ "routing_available": true, "entries": [RoutingEntry…], "content_hash": "sha256:…" }`.
  When `routing_available` is `false` (no performer endpoint mounts a routing YAML), the
  UI shows an explanatory empty state and read-only guidance — not an error.
- **POST /api/config/routing/entry** — create entry. Body: `{ entry, base_hash }`.
- **PUT /api/config/routing/entry/{index}** — update entry. Body: `{ changes, base_hash }`.
- **DELETE /api/config/routing/entry/{index}** — delete. Body: `{ base_hash }`.

**Validation surfaced inline** (mirrors performer enforcement):
- `strategy: reroute` ⇒ `normalizers` must be empty.
- `strategy: normalize` ⇒ `normalizers` non-empty and every name in `NORMALIZER_REGISTRY`.
- `wire_format` ∈ {`openai`, `anthropic`}; `base_url` non-empty.

**All routing writes** → `SaveResult{ applied: "staged_next_job" }` — the change takes
effect on the **next performer job** (the file is read once at job start; no in-flight
injection). The UI labels this clearly (D2/D7).

---

## Error model (all endpoints)

| HTTP | `errors[].code` | When |
|------|-----------------|------|
| 409 | `conflict` | `base_hash` ≠ current file hash (external edit detected, D4) |
| 409 | `referenced` | Deleting a catalog item still referenced (D6) |
| 422 | `validation` | pydantic / cross-catalog / routing-model validation failed |
| 403 | `forbidden` | Attempt to edit a non-editable/derived setting |

`message` is always actionable and secret-free.

**Save round-trip** (validate + atomic write, excluding hot-reload propagation):
< 500 ms p95 (SC-010).
