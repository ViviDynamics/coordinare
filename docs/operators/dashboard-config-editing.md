# Live Config Editing in the Dashboard (spec 081)

The dashboard **Config** view lets you edit three parts of the running coordinare
configuration without hand-editing YAML: **global tuning**, the spec-080 model
catalogs (`endpoints` / `model_endpoints` / `modes`), and the spec-078
self-hosted routing table. Personas and symphonies are **view-only** here — you
can inspect them in the Config view, but their edits are made through the
dedicated Personas and Symphonies UIs/endpoints, not from this view.

## What you can do

- **View everything**: every setting shows a label, help text, type, current
  value, default, and valid range/enum. Read-only/derived settings are visually
  distinct from editable ones.
- **Edit safely**: edits are validated server-side (pydantic) with actionable
  inline errors, written atomically, and guarded by optimistic concurrency.
- **Apply live where safe**: hot-reloadable settings take effect immediately
  (`applied: hot_reloaded`); settings read once at startup are flagged
  **restart-required** (`applied: staged_restart`).
- **Manage catalogs**: full CRUD on `endpoints` / `model_endpoints` / `modes`
  with referential integrity and delete-protection (you can't delete an endpoint
  a model_endpoint still references).
- **Manage routing**: full CRUD on the spec-078 routing table when a performer
  endpoint mounts one.

## Two operator-visible trade-offs (read before you edit)

### 1. Saving strips comments from `config.yaml`

A save rewrites the target YAML via `yaml.safe_dump`, which **does not preserve
file-level comments**. If you save any section through the dashboard, the
comments in your live `config.yaml` are dropped from the rewritten file.

This is by design — the dashboard's labels and help text are now the
documentation surface, and the tracked **`config.example*.yaml`** files retain
the fully commented reference. If you rely on inline comments in a live
`config.yaml`, keep them in the tracked example file, not the running one.

### 2. Routing-table edits bind at the **next** performer job

The routing table the dashboard edits is the **host-side YAML** that performer
endpoints mount into their containers. There is no in-flight injection: a routing
edit **never** affects a running performer job. The file is read once at job
start, so a change takes effect on the **next** job that starts after you save.

Routing saves therefore report `applied: staged_next_job` and the UI labels them
"applies to the next performer job." If no performer endpoint mounts a routing
YAML (`SELFHOSTED_ROUTING_CONFIG` + a matching volume), the routing section shows
a read-only empty state with guidance to wire one up — this is expected, not an
error.

## Secrets

- Secret-flagged values render masked (`••••••`); `${VAR}` env placeholders
  render as the **raw literal** (e.g. `${COORDINARE_GITHUB_TOKEN}`), never the
  expanded value.
- Saving a section without changing a masked secret is a **no-op** for that
  secret — the on-disk `${VAR}` reference is preserved verbatim. The dashboard
  never writes the mask glyphs or an expanded secret to disk, and never returns a
  secret in an API payload or error message.

## Concurrency

Every load captures a content hash of the file. If `config.yaml` (or the routing
YAML) changes on disk after you load the view, your save is rejected with a
**409 conflict** ("changed on disk since you loaded it — reload and re-apply").
Reload the view to pick up the on-disk value, then re-apply your edit.

## Reload failure is safe

If the atomic write succeeds but the live hot-reload then fails, the prior
in-memory configuration is **retained** (no partial swap) — the change is on disk
and the save downgrades to `staged_restart` with an operator-readable message.
Restart the daemon to pick up the persisted change.
