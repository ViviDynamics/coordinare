# Quickstart: Live Config Editing in the Dashboard UI

**Feature**: 081-config-ui | **Date**: 2026-06-06

This walks an operator (and a developer verifying the feature) through the full editable
config surface in the dashboard: global tuning, personas, per-symphony overrides, the
spec-080 model catalogs, and the spec-078 routing table.

## Prerequisites

- Coordinare running with the dashboard on `:9090` / `:9091`
  (`set -a && source .env && set +a` first — `${VAR}` placeholders expand at load time).
- A `config.yaml` present (the repo-root one is used by the daemon).
- For routing-table editing: at least one `performer_endpoint` whose `volumes` mounts a
  routing YAML and whose env sets `SELFHOSTED_ROUTING_CONFIG` to that container path.
  Without it the routing section shows an explanatory empty state (read-only).

## 1. View the whole configuration

1. Open the dashboard → **Config** view.
2. Confirm every section renders: **Global Tuning**, **Personas**, **Symphonies**,
   **Endpoints**, **Model Endpoints**, **Modes**, **Routing Table**.
3. Each setting shows a label, help text, type, current value, default, and valid
   range/enum. Read-only/derived settings are visually distinct from editable ones.
4. Secrets show masked (`••••••`); `${VAR}` placeholders show the **raw literal**
   (e.g. `${COORDINARE_GITHUB_TOKEN}`), never the expanded value.

Backing call: `GET /api/config/all` (returns descriptors + `content_hashes`).

## 2. Edit a hot-reloadable global setting

1. Change `poll_interval_seconds` to a valid value → **Save**.
2. Expect inline success and `applied: hot_reloaded` — no restart. The daemon's
   `config_version` increments.
3. Enter an out-of-range value → **Save** → expect an **inline** error
   ("Must be between 1 and 3600 seconds."), no write performed, no stack trace.

Backing call: `PUT /api/config/section/global`.

## 3. Optimistic-concurrency (external edit) check

1. Load the Config view (captures the baseline hash).
2. In a terminal, edit `config.yaml` by hand and save.
3. Back in the UI, change a field → **Save** → expect a **409 conflict**: "The
   configuration changed on disk since you loaded it. Reload and re-apply."
4. Reload → the new on-disk value is shown → re-apply succeeds.

## 4. Manage the spec-080 model catalogs (full CRUD)

1. **Create** an `endpoint` (e.g. `{name: local-vllm, kind: vllm, base_url: …}`) → Save.
2. **Create** a `model_endpoint` referencing it; try an unknown endpoint name → expect a
   `validation` error naming the missing endpoint.
3. **Create** a `mode` referencing the model_endpoint.
4. **Delete-protection**: try to delete the endpoint while the model_endpoint still
   references it → expect a `409 referenced` error naming the referrer. Delete the
   referrers first, then the endpoint succeeds.

Backing calls: `GET/POST/PUT/DELETE /api/config/catalog/{endpoints|model_endpoints|modes}`.

## 5. Manage the spec-078 routing table (full CRUD)

1. Open **Routing Table**. If `routing_available` is false, you'll see guidance to mount a
   routing YAML in a performer endpoint — wire it, then return.
2. **Create** an entry with `strategy: normalize` and a non-empty `normalizers` list whose
   names are all in the normalizer registry → Save. Expect `applied: staged_next_job` with
   a clear "applies to the next performer job" note.
3. Set `strategy: reroute` but leave `normalizers` non-empty → expect an inline validation
   error ("reroute strategy must have no normalizers").
4. Confirm the edit landed in the **host-side YAML** the performer endpoint mounts:
   ```bash
   cat <host_path_of_the_mounted_routing_yaml>
   ```
5. Confirm the change is picked up by the **next** performer job (not running jobs).

Backing calls: `GET /api/config/routing`,
`POST/PUT/DELETE /api/config/routing/entry[/{index}]`.

## 6. Secret-safety check

1. Inspect the `GET /api/config/all` payload (browser devtools / curl): confirm **no
   secret value** appears in plaintext — secrets are masked, `${VAR}` literals are raw.
2. Save a section containing a masked secret you did **not** edit → confirm the secret is
   preserved unchanged on disk (masked-unchanged = no-op).
3. Grep the dashboard logs for any secret value → expect none.

## 7. Performance sanity (SC-010)

- `GET /api/config/all` returns in < 300 ms p95 server-side for the representative config.
- A single save round-trip (validate + atomic write, excluding reload propagation)
  completes in < 500 ms p95.
- These are enforced by the CI benchmark added with this feature.

## Notes / known trade-offs

- Saving rewrites the target YAML via `yaml.safe_dump`, which **strips file-level
  comments**. The dashboard's labels/help are now the documentation surface; the tracked
  `config.example*.yaml` files retain the commented reference.
- Routing-table edits never affect in-flight performer jobs — they bind at the next job's
  start by design (the file is read once at job start; there is no in-flight injection).
