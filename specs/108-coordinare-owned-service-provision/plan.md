# Implementation Plan: Coordinare-Owned Deterministic Service Provisioning

**Branch**: `108-coordinare-owned-service-provision` | **Spec**: [spec.md](spec.md)

## Key grounding (from code, 2026-06-23)

- Coordinare ALREADY writes `verify.sh` + `activate.sh` + `manifest.json` directly to the HOST cache dir in `EnvCacheService` (`src/coordinare/services/env_cache.py` ~L1036-1052), where `declared_services` is in scope. These appear at `cache_mount_path` inside the container — GUARANTEED persistence (no agent involved).
- The service scripts (`services-{start,stop,health}.sh`) are currently written PERFORMER-side by `apply_manual_override` / `_run_service_inference` — which is exactly the path that intermittently fails to persist (the "`<cache>/services/` empty" bug).
- The service deb fetch+extract is in the PERSONA (`http_performer_service._render_system_services_install`) — delegated to the flaky agent.
- `env_cache_path` passed to the performer IS the container mount path (dispatch_performer.py:1283), so it's not a host/container mismatch — the issue is WHO writes (flaky agent/performer path) vs coordinare writing directly.

## Design (coordinare writes everything; performer just runs)

1. **Coordinare renders + writes the service scripts to the cache** (in `env_cache.py`, alongside activate.sh/verify.sh):
   - `services/services-provision.sh` — NEW `render_services_provision_sh(services, cache_mount_path)` (in `env_manifest.py`, where `_SERVICE_KIND_PACKAGES`/`derive_service_install_items` live): the 106 closure fetch (`apt-get install --download-only -o Dir::State::status=<base-snapshot> -o Dir::Cache::archives=<cache>/debs/ <pkgs>`) + `dpkg-deb -x` extract into `<cache>/services-extract`.
   - `services/services-{start,stop,health}.sh` — render via the existing `coordinare_service_inference.templater.render(manifest)` from the validated declared services, written by coordinare here (deterministic persistence) rather than performer-side.
   - Idempotent + chmod 0o755, mirroring the activate.sh/verify.sh writes.
2. **Performer runs provisioning deterministically** before readiness: new `_provision_env_cache_services(env_cache_path, cache_env)` in `workspace.py` (mirrors `_start_env_cache_services`), invoked in `main.py` env_bootstrap completion in the 107 order: provision → (inference, now redundant for scripts but harmless / can be slimmed) → readiness → verify. A non-zero provision for a REQUIRED service → env-attributed bootstrap failure.
3. **Remove the SYSTEM SERVICES persona block** (`_render_system_services_install` no longer injected into the bootstrap persona) — the agent is no longer asked to fetch service debs.

## Constitution Check
- TDD: render_services_provision_sh tests; env_cache writes-service-scripts tests; performer runs-provision-before-readiness test; persona-no-longer-has-SYSTEM-SERVICES test. PASS.
- Secret-free, no new dep, image service-agnostic. PASS.
- Reuses 106 command, _SERVICE_KIND_PACKAGES, the templater, the 101/107 gate/order. PASS.

## Phasing (incremental within the spec)
- **P1 (MVP, highest value / lowest risk):** coordinare writes `services-{start,stop,health}.sh` to the cache in env_cache.py (fixes the "services/ empty" persistence blocker so readiness can start postgres — binaries already land via the 106 persona fetch). 
- **P2:** coordinare renders+writes `services-provision.sh` + performer runs it deterministically before readiness; remove the persona SYSTEM SERVICES block (fully de-agent the fetch).
- **P3:** idempotency/failure-surface guards + no-op + reconcile the now-redundant performer-side manual_override script write (avoid double-write/conflict).

## Risk note
This touches the critical env-bootstrap path across coordinare (env_cache.py, env_manifest.py), performer (main.py, workspace.py), and the persona. It warrants careful TDD + adversarial review + a live re-bootstrap validation. Recommended as a focused effort (not rushed).
