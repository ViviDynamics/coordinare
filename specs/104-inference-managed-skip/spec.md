# Feature Specification: Inference Skips Run-Validation for Coordinare-Managed Stateful Services

**Feature Branch**: `104-inference-managed-skip`
**Created**: 2026-06-22
**Status**: Draft
**Continues**: 091 (service hosting), 101 (readiness gate), 102 (closure-resolving server fetch), 103 (server-bin on PATH)

## Overview

Service inference proposes a `services.json` manifest by exploring a project and, by default, **validates** that manifest by actually running the rendered `services-start.sh → services-health.sh → services-stop.sh`. For a **coordinare-managed** stateful service (postgres/redis), the server binary does **not exist at inference time** — coordinare installs it from the *declared* manifest later, during env-bootstrap (091/102), and verifies its readiness at bootstrap via the spec-101 gate. So validating such a service by starting it at inference time can never succeed: the start hits a command-not-found server, the bounded readiness wait spins, and the validator's subprocess timeout fires as an uncaught `TimeoutExpired`, which the caller records as an unexpected error and returns **zero services**. The project therefore never declares its database, the server is never fetched, and the bootstrap "succeeds" with no usable service.

This feature makes inference **defer coordinare-managed services' readiness to the spec-101 gate** — it does not start postgres/redis at inference time. It validates only the services it actually *can* validate then (generic, non-external services), exactly mirroring the existing managed-kind exemption already applied to the binary-resolvability check.

## Why

The website's Postgres bootstrap stalls were root-caused (confirmed live 2026-06-22) to this exact path: the LLM produced a correct postgres+redis manifest every time, but the validate-by-starting step timed out identically on two different inference models (gpt-oss:120b and glm-4.7-flash), proving it is **not** a model-capability or model-speed issue but a structural one. An earlier fix exempted coordinare-managed kinds from the *binary-resolvability* check but missed the *run-validation* layer. This is the missing half.

## User Scenarios & Testing

### User Story 1 — Managed-only manifest is recorded, not timed out (Priority: P1) 🎯 MVP

A project declares only coordinare-managed stateful services (e.g. postgres + redis). Inference proposes a valid manifest.

**Why this priority**: This is the failure that blocks the website. Without it, no managed-service project can ever declare its services through inference.

**Acceptance**:
1. **Given** a candidate manifest whose stateful services are all coordinare-managed (postgres/redis), **when** inference runs, **then** it does NOT start/health-check those services, completes without an uncaught `TimeoutExpired`, and records the declared services (writes the success artifacts) so env-bootstrap renders the 102 install block and hosts them.

### User Story 2 — Generic services are still validated (Priority: P2)

A project declares a generic service (a non-managed binary that *is* expected to be present/installable and runnable at inference time).

**Why this priority**: The run-validation exists to catch real start/health regressions; that protection must remain for the services it can legitimately exercise.

**Acceptance**:
1. **Given** a manifest with a generic service, **when** inference runs, **then** the start→health→stop run-validation executes for that generic service and a genuine start/health failure still rejects the manifest (no regression to existing behavior).

### User Story 3 — Mixed manifest validates only the generic part (Priority: P3)

A project declares both managed (postgres) and generic services.

**Acceptance**:
1. **Given** a mixed manifest, **when** inference validates, **then** only the generic service is started/health-checked; postgres/redis are never started; the manifest is accepted when the generic part validates.
2. **Given** a manifest whose only services are coordinare-managed (postgres/redis, nothing left after exclusion), **when** inference runs, **then** run-validation is skipped entirely and the manifest is accepted (not failed, not hung).

### Edge Cases

- A manifest with only `external_required` services → still validated (their start script asserts `required_env_vars`); a missing required env var still rejects the manifest (existing behavior preserved).
- A generic service that genuinely fails to start → still rejected (regression guard).
- An empty manifest (no services) → unchanged behavior.

## Requirements

### Functional Requirements

- **FR-001**: At inference time, the run-validation MUST NOT start or health-check any service whose `kind` is in the existing `COORDINARE_MANAGED_KINDS` frozenset (postgres/redis). Their readiness is coordinare's responsibility post-install, verified by the spec-101 gate at bootstrap.
- **FR-002**: The run-validation (start → health → stop) MUST still execute for all non-managed services — both generic in-container services AND `external_required` services (whose rendered start script only asserts `required_env_vars` are present and never starts a binary, so it is safe and surfaces missing operator config). Only coordinare-managed kinds are exempt.
- **FR-003**: When, after excluding coordinare-managed kinds, NO services remain to validate, the run-validation MUST be skipped and the candidate manifest accepted (success artifacts written) — never failed or left to hang.
- **FR-004**: For a mixed manifest, validation MUST exercise only the non-excluded services; managed services MUST never be started during inference.
- **FR-005**: After this change, a manifest whose only stateful services are coordinare-managed MUST NOT produce the uncaught `TimeoutExpired` → `unexpected_error` → `services=[]` outcome; it MUST record the declared services.
- **FR-006**: The exemption MUST reuse the existing `COORDINARE_MANAGED_KINDS` source of truth (the same frozenset the binary-resolvability check uses) — no parallel definition of "managed."
- **FR-007**: No secret values in logs/records (names/kinds/counts/reasons only); no new external dependency; the service-inference package remains the shared source of truth; persisted state model unchanged.

### Key Entities

- **ServicesManifest / ServiceEntry**: the proposed manifest; each entry's `kind` (generic/postgres/redis) and `external_required` flag determine whether it is validated-by-starting at inference time.
- **COORDINARE_MANAGED_KINDS**: existing frozenset (postgres/redis) — the single source of truth for "coordinare installs + spec-101 verifies this, don't start it at inference time."

## Success Criteria

- **SC-001**: A managed-only manifest (postgres+redis) completes inference and records both services — no `TimeoutExpired`, no empty `services=[]`. (Replays the website failure.)
- **SC-002**: A generic service that fails to start is still rejected by inference (regression guard intact).
- **SC-003**: A mixed manifest records all declared services while never starting postgres/redis during inference.
- **SC-004**: Inference wall-time for a managed-only manifest no longer includes a ~60s+ doomed readiness-wait (the start phase is skipped for managed services).

## Assumptions

- The rendered `services-start.sh`/health/stop templates are unchanged (091); the exemption lives in the inference run-validation gating, not the templater.
- Coordinare-managed service readiness at runtime is already correctly enforced by the merged spec-101 gate — this spec deliberately defers to it.
- Generic services are expected to have their binaries resolvable at inference time (the binary-resolvability check already enforces this for non-managed kinds).

## Out of Scope

- Host-resolution / DNS aliasing of service hostnames to 127.0.0.1 (separate spec — approach C).
- Authoring `.coordinare/score.json` for any project (config/manual-override).
- The spec-101 readiness gate itself (already merged — the runtime verifier this spec defers to).
- Any change to the services-start/health/stop templater (091).
- Inference LLM model/provider choice; non-Debian package managers.
