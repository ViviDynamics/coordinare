# Research: Env-Bootstrap Service-Readiness Completion Gate

## D1 — Where the gate runs

**Decision**: In `agent/performer/src/performer/main.py`, the `if perf.role == "env_bootstrap"` completion branch, **after** `_run_service_inference` and **before** `perf.state = "env_bootstrap_complete"`. Insert a service-readiness step that, for **required declared services**, starts them and confirms connectability; on failure it returns `PerformerResponse(status="error", reason=...)` — the same shape the existing `verify.sh` failure (077) already uses.

**Rationale**: this is the single point where the cache is declared complete, right where verify.sh (toolchain) is already gated. The services + their 091 scripts live in the cache at this moment. A failure here rides the existing bootstrap-error path with zero new coordinare wiring.

**Alternatives rejected**: a coordinare-side post-bootstrap probe — the coordinare can't connect to services inside the per-job cache; the performer is where they run.

## D2 — Start + connectability mechanism: reuse the 091 scripts

**Decision**: Reuse the existing `services-start.sh` + `services-health.sh` (091/063) that service inference already drops into `<cache>/services/`. `workspace.py` already has the invocation helpers (`run_services_start`, the services-health non-zero flag, `consume_services_start_failure`). The readiness gate = run `services-start.sh`, then `services-health.sh`, and require exit 0. `services-health.sh` is the per-service connect/health check (e.g. `pg_isready` / a DB connect) the manifest already generates — so "connectable" is exactly a clean health exit.

**Rationale**: these scripts already encode each service's start + health/connect; the gap is only that they were run **per-job** (063) and never as a **bootstrap-completion gate**. Reusing them avoids a bespoke per-service probe and stays consistent with how services run later.

**Alternatives rejected**: a new per-kind connect probe in the gate — duplicates `services-health.sh`; reuse it.

## D3 — Required services + rejected manifest

**Decision**: The set of declared services comes from the same source service inference uses — `.coordinare/score.json` (→ `declared_services` / the manifest's `services`). Treat a declared service as **required by default** (it's declared because the tests need it); honor an explicit optional/best-effort flag if present. Then:
- **Manifest rejected/empty** (`inference_succeeded` False, `services.json.rejected` written) **AND** required services were declared → **bootstrap fails** with the rejection reason (e.g. "services manifest rejected: postgres binary unresolvable").
- **Manifest present** → run start + health; a required service whose start/health fails → **bootstrap fails** naming the service.
- **No declared services** → skip the gate entirely (unchanged).

**Rationale**: the live failure was precisely "required services declared (postgres) + manifest rejected + marked complete." Tying the gate to the declaration set makes it opt-in and targeted.

## D4 — Failure rides the existing bootstrap-error → not-ready path

**Decision**: A readiness failure sets `perf.state = "error"` + a reason, returning the error `PerformerResponse`. The coordinare's `on_bootstrap_complete(success=False)` already leaves `readme_sha=None` and does NOT flip `cache_dir_ready` → the cache is **not ready**, the bootstrap re-dispatches, and the 093 readiness gate / 088 state keep cards from dispatching. The operator-facing reason (service + cause) surfaces via the existing ENV_BLOCKED / bootstrap-failure notification surface.

**Rationale**: no new coordinare mechanism — a service-readiness failure is handled identically to a verify.sh failure (already wired). The change is making the performer EMIT that failure for unconnectable required services.

## D5 — Bounded + secret-free

**Decision**: `services-start.sh`/`services-health.sh` run under the existing bounded timeout, with a brief bounded retry on the health check (a slow-to-accept DB shouldn't false-fail; a genuinely-down one surfaces within the bound). Reasons/records carry only service **name + status + cause** (e.g. "postgres: not connectable", "manifest rejected: binary unresolvable") — never DB passwords or raw output.

**Rationale**: FR-006/FR-008 carried from 091/095; the health scripts already avoid echoing secrets.

## D6 — Persisted readiness (optional, backward-compatible)

**Decision**: If useful for the dashboard/operator signal, extend `EnvCacheState` with an optional per-service readiness list (name/status/reason), backward-compatible default empty. Not required for the gate itself (the gate acts at bootstrap time); only for surfacing. Keep minimal.

**Rationale**: the gate's job is to block; persistence is for observability. Add only if it improves the operator signal without a schema-version risk.
