# Phase 1 Data Model: Live Config Editing in the Dashboard UI

**Feature**: 081-config-ui | **Date**: 2026-06-06 | **Plan**: [plan.md](./plan.md)

This feature introduces **no new persisted schema**. The persisted truth remains
`config.yaml` (coordinare config + spec-080 catalogs) and the performer routing-table
YAML referenced by `SELFHOSTED_ROUTING_CONFIG`, both validated by their existing
pydantic models. The entities below are the **server-computed view/transfer objects**
the descriptor layer produces and the save endpoints consume — they live in
`config_descriptors.py` (read side) and `config_write_service.py` /
`routing_config_service.py` (write side).

---

## E1. ConfigSetting (descriptor)

The atomic unit the UI renders. Derived per-field from the pydantic models (research D1).

| Field | Type | Source / Notes |
|-------|------|----------------|
| `key` | str | Dotted path, e.g. `global.poll_interval_seconds`, `modes[single-qwen].tool` |
| `label` | str | Human-readable; from annotation table, falls back to title-cased field name |
| `help` | str \| null | From `Field(description=...)` on the model |
| `type` | enum(`string`,`int`,`float`,`bool`,`enum`,`list`,`text`,`secret`) | Inferred from the field annotation |
| `current_value` | any \| null | Current parsed value; **masked** if `secret` and value is a literal (research D3) |
| `default` | any \| null | From `FieldInfo.default` / `default_factory` |
| `range` | {min,max} \| null | From `ge`/`le`/`gt`/`lt` constraints (numeric) or `min_length`/`max_length` |
| `enum` | list[str] \| null | From `Literal[...]` members or `Enum` |
| `editable` | bool | Annotation table; read-only/derived → `false` |
| `restart_required` | bool | Annotation table (research D7) |
| `secret` | bool | Annotation table (research D3) |
| `is_env_placeholder` | bool | True when the raw value is a `${VAR}` literal (research D3) |
| `section` | str | Owning ConfigSection id |
| `store` | enum(`config_yaml`,`routing_yaml`) | Which backing file persists this setting |

**Validation rules**: descriptors are read-only projections — validation happens on the
*write* side against the pydantic models, not on the descriptor itself.

---

## E2. ConfigSection

A logical grouping of settings for the UI. Pure presentation/organization.

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | Stable id, e.g. `global`, `personas`, `symphonies`, `endpoints`, `model_endpoints`, `modes`, `routing` |
| `title` | str | Display title |
| `description` | str \| null | Section help |
| `store` | enum(`config_yaml`,`routing_yaml`) | Backing file |
| `kind` | enum(`scalar_group`,`collection`) | `scalar_group` = fixed set of fields (global); `collection` = list of editable entities (catalogs, routing, personas, symphonies) |
| `settings` | list[ConfigSetting] | For `scalar_group` sections |
| `items` | list[CatalogItem] | For `collection` sections |

**Sections** (maps the full editable surface — the spec's coverage requirement):
1. `global` — expanded coordinare global tuning (beyond today's 11-field allow-list)
2. `personas` — per-role persona instructions (existing CRUD)
3. `symphonies` — per-symphony overrides
4. `endpoints` — spec-080 endpoints catalog
5. `model_endpoints` — spec-080 model_endpoints catalog
6. `modes` — spec-080 modes catalog
7. `routing` — spec-078 routing table (store = `routing_yaml`)

---

## E3. CatalogItem

One entity in a `collection` section (a persona, a symphony override, an `endpoint`,
`model_endpoint`, `mode`, or routing `entry`). Carries its own descriptors plus
reference/usage metadata for delete-protection.

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | Natural key (e.g. endpoint `name`, mode `name`, persona `role`) |
| `kind` | str | Section id it belongs to |
| `settings` | list[ConfigSetting] | The item's editable fields |
| `referenced_by` | list[str] | Other items referencing this one (research D6); non-empty ⇒ delete-protected |
| `deletable` | bool | `false` while `referenced_by` is non-empty |

---

## E4. RoutingTable / RoutingEntry / TargetDescriptor (spec-078, reused)

Reused verbatim from `agent/performer/src/performer/proxy/routing.py` for coordinare-side
validation parity (research D2). Not redefined here; the dashboard validates proposed
routing edits by constructing these models, and a validation failure becomes an inline
UI error.

- `RoutingTable { entries: list[RoutingEntry] }`
- `RoutingEntry { backend, model, target: TargetDescriptor }` (backend kebab→snake normalized)
- `TargetDescriptor { base_url, wire_format: openai|anthropic, strategy: normalize|reroute,
  normalizers: list[str], reroute_upstream }` — validator: `reroute` ⇒ empty normalizers;
  `normalize` ⇒ non-empty normalizers all in `NORMALIZER_REGISTRY`.

---

## E5. ConfigSnapshot (read response envelope)

What a "read the whole config" call returns.

| Field | Type | Notes |
|-------|------|-------|
| `sections` | list[ConfigSection] | Full editable surface |
| `content_hashes` | {config_yaml: str, routing_yaml: str \| null} | SHA-256 baselines for optimistic concurrency (research D4) |
| `config_version` | int | Current daemon config_version (for client awareness) |
| `routing_available` | bool | False when no performer endpoint mounts a routing YAML (D2) |

---

## E6. SaveRequest / SaveResult (write side)

| SaveRequest field | Type | Notes |
|-------------------|------|-------|
| `store` | enum(`config_yaml`,`routing_yaml`) | Target file |
| `section` | str | Section being saved |
| `changes` | map<key, value> | Edited settings only; masked-unchanged secrets omitted (D3) |
| `base_hash` | str | Optimistic-concurrency precondition (D4) |

| SaveResult field | Type | Notes |
|------------------|------|-------|
| `ok` | bool | |
| `applied` | enum(`hot_reloaded`,`staged_restart`,`staged_next_job`) | How the change takes effect (D7/D2) |
| `new_hash` | str | New baseline after write |
| `errors` | list[FieldError] | On validation/concurrency failure (see below) |

| FieldError field | Type | Notes |
|------------------|------|-------|
| `key` | str \| null | Offending setting (null = whole-form/concurrency error) |
| `code` | enum(`validation`,`conflict`,`referenced`,`forbidden`) | |
| `message` | str | Actionable, human-readable — never a raw stack trace (UX Principle III) |

---

## State transitions (save lifecycle)

```
load → ConfigSnapshot{content_hashes}
edit (client-side) → SaveRequest{changes, base_hash}
save →
  ├─ base_hash mismatch        → SaveResult{ok:false, errors:[conflict]}        (D4)
  ├─ pydantic/cross-ref invalid → SaveResult{ok:false, errors:[validation]}      (D6)
  ├─ delete of referenced item → SaveResult{ok:false, errors:[referenced]}       (D6)
  └─ valid → atomic write (D5) →
        ├─ config_yaml + hot-reloadable → trigger reload → applied=hot_reloaded   (D7)
        ├─ config_yaml + restart field  → applied=staged_restart                  (D7)
        └─ routing_yaml                 → applied=staged_next_job                  (D2)
```
