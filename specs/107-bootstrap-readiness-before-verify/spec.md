# Feature Specification: Start Declared Services Before Running verify.sh

**Feature Branch**: `107-bootstrap-readiness-before-verify`
**Created**: 2026-06-22
**Status**: Draft
**Fixes**: a spec-093 (verify.sh live service probe) vs spec-101 (service-readiness gate) ordering bug

## Overview

The performer's env_bootstrap completion runs, in order: (1) `run_env_cache_verify` (verify.sh) → (2) `_run_service_inference` (writes services-start.sh) → (3) `run_service_readiness` (STARTS + health-checks declared services). But spec-093's `_service_readiness_check` injects a LIVE `pg_isready`/`redis-cli PING` probe into verify.sh that hard-FAILs when the service isn't running. So verify.sh (step 1) fails on the service probe before anything has started the service, returns an error early, and steps 2–3 never run. Spec-101's readiness gate — which is what actually starts the services — is unreachable for any declared service that isn't already running.

This feature **reorders** the completion so the services are started first: inference (writes the start scripts) → readiness (starts + health-checks the services) → verify.sh (toolchain + the live service probe, which now sees the running services).

## Why

Confirmed live (2026-06-22): with postgres+redis declared (score.json), fetched (102/106), and on PATH (103), the bootstrap still failed `FAIL: service postgres not accepting connections on 45432 (pg_isready)`. Root cause: verify.sh ran before the readiness gate started postgres. The `services/` dir was empty (inference, which writes services-start.sh, never ran — verify returned first).

## User Scenarios & Testing

### User Story 1 — declared services are started before verify checks them (Priority: P1) 🎯 MVP
**Acceptance**:
1. **Given** a declared postgres service, **when** the bootstrap completes, **then** `run_service_readiness` (which starts the service) runs BEFORE `run_env_cache_verify`, so verify.sh's live `pg_isready` probe sees a running service and passes.

### User Story 2 — failure semantics preserved (Priority: P2)
**Acceptance**:
1. A required service that cannot start still fails the bootstrap (readiness returns not-ok → error) — before verify.
2. A non-zero verify.sh (toolchain/install failure) still fails the bootstrap — now after readiness.
3. A missing verify.sh is still treated as degraded (logged, not fatal).
4. No declared services → readiness no-ops `(True, [])`; verify runs as before.

## Requirements

- **FR-001**: The env_bootstrap completion MUST run `_run_service_inference` then `run_service_readiness` (start + health declared services) BEFORE `run_env_cache_verify`.
- **FR-002**: `run_service_readiness` MUST leave started services running so verify.sh's live probe observes them in the same container/session.
- **FR-003**: All existing terminal semantics MUST be preserved: readiness-not-ok → error; verify non-zero → error; verify missing → degraded/proceed; inference timeout → best-effort (defer to readiness); no declared services → unchanged.
- **FR-004**: No change to verify.sh content, the 093 probe, the 101 gate, or any schema. Reorder only. No new dependency. Secret-free.

## Success Criteria

- **SC-001**: For a declared postgres service, `run_service_readiness` is invoked before `run_env_cache_verify` (asserted by call-order).
- **SC-002**: The website re-bootstrap (postgres+redis via score.json) completes `success=True` with a live DB (`pg_isready` passes inside verify after readiness started it).
- **SC-003**: Existing verify-fail / readiness-fail / verify-missing / no-services tests still pass.

## Out of Scope

- The alternative fix (removing the 093 probe from verify.sh) — this spec is the reorder (approach A).
- 102–106 / score.json (the binary-delivery + declaration chain, already merged & proven).
