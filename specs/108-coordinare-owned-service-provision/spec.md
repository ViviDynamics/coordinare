# Feature Specification: Coordinare Deterministically Provisions Declared Services

**Feature Branch**: `108-coordinare-owned-service-provision`
**Created**: 2026-06-23
**Status**: Draft
**Supersedes (for the service path)**: the persona-delegated SYSTEM SERVICES fetch (102/106 persona block); builds on 091 (service scripts), 101/107 (readiness gate + ordering)

## Overview

Today the env-bootstrap delegates the stateful-service **deb fetch + extract** to the LLM bootstrap agent via a persona instruction (the "SYSTEM SERVICES" block). Live runs proved this is unreliable: the agent improvises (skips the fetch, fetches a minimal subset, or litters the cache with garbage dirs), and even the coordinare-written `services-start.sh` intermittently fails to land in `<cache>/services/`. The result is that the declared services' binaries and/or start scripts are missing, so the services never start and the spec-101 readiness gate (correctly) fails the bootstrap.

This feature makes coordinare **own the entire service-provisioning path deterministically**, the same way it already owns `activate.sh`, `verify.sh`, and `services-start.sh`: coordinare renders a `services-provision.sh` (the closure-resolving deb fetch + extract from 106) and the performer runs it — and the service scripts — via deterministic code (not the agent persona) during env-bootstrap, before the readiness gate. The LLM agent is removed from the stateful-service path entirely.

## Why

The binary-delivery fixes (102/103/106) and the ordering fix (107) are all correct and proven in isolation (postgres 17.10 loads from the cache). But the live website bootstrap still fails because the agent-driven steps are unreliable and the service scripts don't reliably persist. Per the project's durable-contract principle (forgetful/flaky models need coordinare-owned contracts, not persona instructions), the fix is to take the agent out of the loop for service provisioning.

## User Scenarios & Testing

### User Story 1 — declared services are provisioned deterministically (Priority: P1) 🎯 MVP

A project declares postgres + redis (score.json). The bootstrap runs.

**Acceptance**:
1. **Given** declared coordinare-managed services, **when** env-bootstrap runs, **then** coordinare (not the LLM agent) fetches the closure-resolving server debs (106 command) + extracts them into the cache, and writes `services-{provision,start,stop,health}.sh` into `<cache>/services/` — reliably, every run.
2. **Given** the provisioned cache, **when** the readiness gate runs, **then** the server binaries are present + on PATH and the services start and are connectable (`pg_isready`/PING) → bootstrap completes.

### User Story 2 — the agent is no longer asked to fetch service debs (Priority: P2)

**Acceptance**:
1. **Given** declared services, **when** the bootstrap persona is built, **then** it no longer contains the "SYSTEM SERVICES" deb-fetch instruction (coordinare does it deterministically); the persona is not relied on for service provisioning.

### User Story 3 — idempotent + ordered + safe (Priority: P3)

**Acceptance**:
1. Provisioning runs BEFORE the readiness gate (107 ordering preserved: provision → readiness → verify).
2. Re-running provisioning is idempotent (already-fetched debs / already-extracted binaries are not corrupted; a re-bootstrap converges).
3. No declared services → provisioning is a no-op (behavior unchanged).
4. A provisioning failure (e.g. apt error) is surfaced as an env-attributed bootstrap failure (not a silent success), routed through the existing on_bootstrap_complete(success=False) path.

### Edge Cases

- External-required services contribute no provisioning (nothing to host).
- Generic services bring their own binary (project install), not coordinare-provisioned.
- A partial prior provision (interrupted) re-converges on the next bootstrap.

## Requirements

### Functional Requirements

- **FR-001**: Coordinare MUST fetch + extract the declared coordinare-managed services' server debs DETERMINISTICALLY during env-bootstrap (performer-side code running a coordinare-rendered script), NOT via an LLM persona instruction.
- **FR-002**: The fetch MUST use the 106 closure-resolving, base-state-independent command (`apt-get install --download-only -o Dir::State::status=<base-snapshot> -o Dir::Cache::archives=<cache>/debs/ <pkgs>`) and extract via `dpkg-deb -x` into the cache, so the versioned server + its full runtime-lib closure (incl ICU) land and are discoverable on PATH via activate.sh.
- **FR-003**: The service scripts (`services-{start,stop,health}.sh` + the new `services-provision.sh`) MUST reliably persist into `<cache>/services/` every bootstrap (no dependence on agent behavior).
- **FR-004**: Provisioning MUST run BEFORE the spec-101 readiness gate (preserving 107's inference/provision → readiness → verify order).
- **FR-005**: Provisioning MUST be idempotent and best-effort-logged; a hard provisioning failure for a REQUIRED service MUST fail the bootstrap (env-attributed), not silently complete.
- **FR-006**: The bootstrap persona MUST NOT instruct the agent to fetch service debs (remove the SYSTEM SERVICES block); the agent remains responsible only for the project toolchain.
- **FR-007**: No declared services → no-op. Invariants: secret-free logs (names/kinds/counts only); no new external dependency (apt + dpkg-deb); base image stays service-agnostic; the kind→package mapping + score.json remain the config surfaces.

### Key Entities

- **services-provision.sh**: coordinare-rendered deterministic fetch+extract script (new), run by the performer before readiness.
- **`_provision_env_cache_services`** (performer): deterministic runner (mirrors `_start_env_cache_services`).
- **kind→package mapping** (`_SERVICE_KIND_PACKAGES`) + `COORDINARE_MANAGED_KINDS`: existing source of truth for which packages a kind needs.

## Success Criteria

- **SC-001**: For a declared postgres+redis project, env-bootstrap completes `success=True` with both services started + connectable — driven entirely by coordinare (no agent involvement in provisioning). Replays the website case to green.
- **SC-002**: The bootstrap persona contains no SYSTEM SERVICES deb-fetch instruction.
- **SC-003**: `<cache>/services/` reliably contains the service scripts after every bootstrap; `<cache>/debs/` contains the versioned server + runtime libs (incl libicu).
- **SC-004**: Re-bootstrap is idempotent (no duplicate/corrupt state); no-declared-services is a no-op.

## Assumptions

- The performer container is Debian-family with the 106 base-state snapshot present (`/opt/coordinare-base-dpkg-status`).
- activate.sh already surfaces extracted server bins on PATH (103) and `*_HOST` aliases (105); the readiness gate (101) starts + health-checks (107 ordering).
- The kind→package mapping is available to the rendering layer (reused from 091/102).

## Out of Scope

- The project toolchain install (still the agent's job; only stateful-service provisioning moves to coordinare).
- Non-Debian package managers; version pinning.
- Replacing inference for service DECLARATION (score.json/manual-override already deterministic); this is about PROVISIONING the declared services.
- 102–107 (merged); this hardens the execution path they established.
