# Data Model: Env-Bootstrap Service-Readiness Completion Gate

**No new store; no breaking schema change.** The gate acts at bootstrap completion (performer-side) and rides the existing bootstrap-success / cache-ready signals.

## 1. Inputs (existing, reused)

| Entity | Source | Use |
|---|---|---|
| **Declared services** | `.coordinare/score.json` → `EnvCacheState.declared_services` (091 ServiceEntry dumps: `name`, `kind`, optional required/optional flag) | the set the gate verifies; **required by default** |
| **Service inference result** | `_run_service_inference` return (`inference_succeeded`, `inference_services`, `inference_skipped_reason`) + `<cache>/services/services.json` or `services.json.rejected` | tells the gate whether a usable manifest exists |
| **091 service scripts** | `<cache>/services/services-start.sh`, `services-health.sh` | start + connect/health mechanism the gate runs |

## 2. Service-readiness result (transient; optionally persisted)

Per declared service, the gate computes: `installed` (binary resolvable) → `initialized` → `started` → `connectable` (health exit 0), plus a secret-free `reason` on failure (e.g. `"postgres: not connectable"`, `"manifest rejected: binary unresolvable"`). Drives the bootstrap outcome; carries no secret values (FR-008).

## 3. Gate → bootstrap outcome

```
env_bootstrap: verify.sh ok → _run_service_inference
        │
   declared required services?
        │ no → env_bootstrap_complete (UNCHANGED)
        │ yes
   manifest produced?
        │ no/rejected → perf.state="error"  reason="required services declared but manifest rejected: <reason>"
        │ yes
   run services-start.sh + services-health.sh (bounded + brief retry)
        │ all required connectable → env_bootstrap_complete
        │ a required service not started/connectable → perf.state="error"  reason="service not connectable: <name>"
        (optional service failing health → warn, continue)
```

A `perf.state="error"` return → coordinare `on_bootstrap_complete(success=False)` → `readme_sha=None`, `cache_dir_ready` NOT set → cache **not ready/dispatchable** (093) → re-bootstrap + ENV_BLOCKED-style operator surface (095).

## 4. Persisted state (reused; optional extension)

- Reused: `EnvCacheState.last_bootstrap_succeeded`, `cache_dir_ready`, `readme_sha` (088) — now also gated on required-service readiness via the bootstrap success/failure signal.
- Optional, backward-compatible: a per-service readiness list on `EnvCacheState` (name/status/reason) for the dashboard/operator signal — empty default, no schema-version risk. Only if it improves surfacing.
