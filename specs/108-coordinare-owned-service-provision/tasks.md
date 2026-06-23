# Tasks: Coordinare-Owned Deterministic Service Provisioning

**Tests**: INCLUDED (Constitution II). Incremental within the spec.

## Phase 1 — P1 MVP: coordinare writes the service scripts to the cache (coordinare-side, no image rebuild)
- [X] T001 Add `write_service_scripts(cache_dir, service_models)` (env_cache.py): render services-{start,stop,health}.sh + services.json via coordinare_service_inference templater; write to <cache>/services/ + chmod; best-effort (log+skip on error); no-op when empty.
- [X] T002 Call it from `_build_manifest_artifacts` alongside the verify.sh/activate.sh writes (coordinare writes directly to the host cache → guaranteed persistence).
- [X] T003 Tests: writes start/stop/health (start has initdb/pg_isready/redis-server), services.json sidecar, executable bits; no-services no-op.
- [ ] T004 Adversarial review + CI + merge; deploy (daemon restart, no image rebuild) + re-bootstrap → confirm <cache>/services/services-start.sh present and readiness starts postgres.

## Phase 2 — P2: deterministic provision (fetch+extract) + de-agent the persona
- [ ] T005 `render_services_provision_sh(services, cache_mount_path)` (env_manifest.py): the 106 closure fetch (Dir::State::status base-snapshot) + dpkg-deb extract; coordinare writes services-provision.sh to the cache.
- [ ] T006 Performer `_provision_env_cache_services` (workspace.py) runs it deterministically before readiness (107 order: provision → readiness → verify); REQUIRED-service provision failure → env-attributed bootstrap error.
- [ ] T007 Remove the SYSTEM SERVICES block from the bootstrap persona (_render_system_services_install no longer injected); the agent no longer fetches service debs.
- [ ] T008 Tests + adversarial review; rebuild images (performer-side) + re-bootstrap → end-to-end green.

## Phase 3 — P3: hardening
- [ ] T009 Idempotency/failure-surface guards; reconcile the now-redundant performer-side manual_override script write (avoid double-write/conflict); no-op + external/generic edge cases.
