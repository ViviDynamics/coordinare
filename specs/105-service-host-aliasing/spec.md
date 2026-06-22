# Feature Specification: Resolve Declared Service Hostnames to Loopback In-Container

**Feature Branch**: `105-service-host-aliasing`
**Created**: 2026-06-22
**Status**: Draft
**Continues**: 091 (service hosting), 092 (test-env injection), 101/102/103/104 (server delivery + declaration)

## Overview

Coordinare hosts a project's declared stateful services (postgres/redis) **inside the performer container, on `127.0.0.1`**, and runs the project's tests in that same container. But a project's test-env (`.env.test`, injected by spec-092) names those services by their **docker-compose service hostnames** — e.g. `POSTGRESQL_HOST=db`, `REDIS_HOST=redis`, and `REDIS_SESSION_STORE_URL=redis://redis:46379/...`. There is no compose network in coordinare's single-container model and nothing maps those names to loopback, so the app resolves `db`/`redis` and fails to connect — even when the server is installed, on PATH, and running.

This feature makes coordinare **alias declared service hostnames to `127.0.0.1`** in the container's `/etc/hosts` at environment-activation time, so the app connects to the coordinare-hosted service regardless of whether the hostname appears in a standalone env var or embedded in a URL.

## Why

This is the final gap in the website Postgres chain. 102 fetches the server, 103 puts it on PATH, 104 makes inference declare it, the spec-101 gate starts it — but the app still can't reach it because `POSTGRESQL_HOST=db` doesn't resolve in-container. It is fundamentally a **name-resolution** problem: an env-var override would miss the hostname embedded in `REDIS_SESSION_STORE_URL=redis://redis:.../`, whereas aliasing the **name** `redis`→`127.0.0.1` fixes both the standalone var and the URL.

## User Scenarios & Testing

### User Story 1 — App reaches the coordinare-hosted DB by its compose hostname (Priority: P1) 🎯 MVP

The project's test-env sets `POSTGRESQL_HOST=db` (and `REDIS_HOST=redis`); coordinare hosts postgres/redis on `127.0.0.1`.

**Why this priority**: Without it, every coordinare-hosted stateful service is unreachable by the app, so DB-backed tests fail "connection refused" even with a running server.

**Acceptance**:
1. **Given** a test-env whose `*_HOST` value is a single-label hostname (e.g. `db`), **when** the environment is activated in-container, **then** `/etc/hosts` contains `127.0.0.1 db` so the app resolves `db` to the coordinare-hosted service.

### User Story 2 — URL-embedded hostnames resolve too (Priority: P2)

The test-env has `REDIS_SESSION_STORE_URL=redis://redis:46379/5/session` and `REDIS_HOST=redis`.

**Acceptance**:
1. **Given** the hostname `redis` is declared by a `*_HOST` var, **when** activation aliases `redis`→`127.0.0.1`, **then** the URL-embedded `redis://redis:.../` also resolves to loopback (no separate URL parsing needed — resolution is by name).

### User Story 3 — Don't clobber real hosts or duplicate entries (Priority: P3)

**Acceptance**:
1. **Given** a `*_HOST` value that is an IP address, `localhost`, `127.0.0.1`, an FQDN (contains a dot), or empty, **when** activation runs, **then** it is NOT aliased (only single-label, non-loopback, non-IP names are).
2. **Given** `/etc/hosts` already contains an entry for a name, **when** activation runs again, **then** no duplicate line is added (idempotent).

### Edge Cases

- No `*_HOST`/`*_HOSTNAME` vars in the environment → `/etc/hosts` unchanged.
- `/etc/hosts` not writable (non-root) → best-effort, non-fatal (the alias block must not abort activation).
- The same single-label name in multiple vars → aliased once.

## Requirements

### Functional Requirements

- **FR-001**: At environment activation (the coordinare-owned `activate.sh`, sourced in-container with the live test-env present), coordinare MUST append `127.0.0.1 <name>` to `/etc/hosts` for each single-label hostname found in the environment's service-host variables, so declared service hostnames resolve to the coordinare-hosted services on loopback.
- **FR-002**: The source of hostnames MUST be the values of environment variables whose name ends in `_HOST` or `_HOSTNAME` (the conventional service-host vars). Aliasing the name covers both standalone vars and URL-embedded references to the same name (no URL parsing).
- **FR-003**: A value MUST be aliased ONLY if it is a single-label hostname: it MUST be skipped when it is empty, `localhost`, an IP address (matches an all-numeric/`:`-bearing pattern), or an FQDN (contains a dot).
- **FR-004**: The operation MUST be idempotent — a name already resolvable via `/etc/hosts` MUST NOT be appended again (safe across re-activation).
- **FR-005**: The operation MUST be best-effort and non-fatal: a non-writable `/etc/hosts` (or any failure) MUST NOT abort activation or fail the env-cache.
- **FR-006**: The aliasing logic MUST read hostnames from the **live environment at runtime** (never bake test-env values into the rendered script) — preserving the secret invariant (the rendered `activate.sh` carries the generic loop, not any test-env value). Logs/records carry no secret values.
- **FR-007**: No new external dependency; no schema change; the base image stays service-agnostic. The change is to the coordinare-owned `activate.sh` render only.

### Key Entities

- **activate.sh** (rendered by `render_activate_sh`, env_manifest.py): coordinare-owned, sourced in-container; the home of the runtime alias block.
- **service-host env vars**: `*_HOST` / `*_HOSTNAME` variables in the injected test-env; their single-label values are the names to alias.

## Success Criteria

- **SC-001**: After activation with `POSTGRESQL_HOST=db`, `getent hosts db` (or a connection to `db`) resolves to `127.0.0.1`. (Replays the website failure.)
- **SC-002**: An FQDN/IP/`localhost`/empty `*_HOST` value is never added to `/etc/hosts`.
- **SC-003**: Re-running activation does not create duplicate `/etc/hosts` entries.
- **SC-004**: The rendered `activate.sh` contains the generic alias loop but no literal test-env hostname value (secret-free); a non-writable `/etc/hosts` does not abort activation.

## Assumptions

- The performer container runs as root with a writable `/etc/hosts` (verified 2026-06-22); the block is still best-effort if that ever changes.
- The injected test-env (spec-092) is present in the environment when `activate.sh` is sourced for the QA/test run.
- Every coordinare-hosted service the app must reach is named by some `*_HOST` var in the test-env (the website case: `POSTGRESQL_HOST`, `REDIS_HOST`). URL-only hostnames that have no corresponding `*_HOST` var are out of scope (none observed).

## Out of Scope

- Parsing hostnames out of URL-shaped vars (unnecessary — the same name appears in a `*_HOST` var).
- A catch-all single-label DNS resolver (rejected: masks typos, applies image-wide).
- Sidecar/docker-in-docker services (rejected: undoes the cache-based hosting design).
- Inference, server fetch/PATH, the readiness gate (101/102/103/104 — already merged).
- Non-Debian base images; rootless `/etc/hosts` alternatives (HOSTALIASES) unless root ever goes away.
