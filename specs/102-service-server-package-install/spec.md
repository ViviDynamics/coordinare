# Feature Specification: Env-Bootstrap Delivers Runnable Service Server Binaries

**Feature Branch**: `102-service-server-package-install`  
**Created**: 2026-06-22  
**Status**: Draft  
**Input**: User description: "The env-bootstrap must install the package(s) that actually contain a declared service's runnable server binaries — resolving meta-packages to the concrete server package — so a declared service like Postgres can actually run."

## Overview

When a symphony declares a stateful service (e.g. a database), the coordinare maps the service to system package(s) the env-bootstrap downloads into the environment cache, so the cache carries the service binaries while the base performer image stays service-agnostic. But the mapping names a **meta-package**, and the download doesn't resolve that meta-package's dependency on the **concrete server package** — so the runnable server binaries never land in the cache.

This is the root data-gap behind the website Postgres failure (2026-06-22): the mapping fetched the Postgres *meta-package* (which is architecture-independent and contains **no binaries** — it only declares a dependency on the versioned server) plus the *client*, but **not the versioned server package** that actually ships the database daemon and its init tools. So the cache had a Postgres "install" with **zero server binaries** — no way to create a data cluster, nothing to start, and the service-setup step rejected it because the server binary was unresolvable. With the spec-101 readiness gate this now correctly **fails the bootstrap**, but the durable fix is to make the install actually deliver a runnable server.

This feature makes the service-binary install **deliver the runnable server**: for a declared service of a known kind, fetch the package(s) that contain its server binaries — resolving a meta-package to the concrete server package(s) it depends on — so after bootstrap the service's key binaries are present and runnable in the cache, and the service can actually start and accept connections.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A declared service's server binaries are actually installed (Priority: P1)

When a symphony declares a service of a known kind (e.g. a database), the env-bootstrap fetches the package(s) that contain the service's runnable **server** binaries — not just a contents-free meta-package or the client — so the cache can actually run that service.

**Why this priority**: This is the root fix — without runnable server binaries the service can never start, the bootstrap (rightly, per 101) fails, and every dependent card is blocked. It is the MVP.

**Independent Test**: Bootstrap a cache for a symphony declaring a database service; verify the service's key server binaries (e.g. the cluster-init + daemon tools) are present and runnable in the activated cache — not merely a meta-package with no binaries.

**Acceptance Scenarios**:

1. **Given** a symphony declares a database service, **When** the env-bootstrap runs, **Then** the cache contains the service's runnable server binaries (init + daemon tools), resolvable on the cache's path.
2. **Given** the kind's mapped package is a meta-package (no binaries of its own), **When** the install runs, **Then** the concrete server package it depends on is also fetched and installed, so the binaries are present.
3. **Given** the binaries are present, **When** the spec-101 readiness gate runs, **Then** the service starts and is connectable and the bootstrap completes (the gate passes for a correctly-installed service).

---

### User Story 2 - The install is resilient across distro versions (Priority: P2)

The server package resolution doesn't hard-pin one distro version that silently breaks when the base image advances; it resolves whatever concrete server package the kind currently requires.

**Why this priority**: A brittle literal version (e.g. a hard-coded major version) would re-break the install the next time the base image's distro moves — re-introducing this exact failure. Important, but rides on US1.

**Independent Test**: Resolve the server package on the current base image, then on a bumped base image (or a simulated version change); verify the correct concrete server package is fetched in both, without a code edit hard-pinned to one version.

**Acceptance Scenarios**:

1. **Given** the base image provides distro version *N*, **When** the install resolves the service's server package, **Then** it fetches the server package that distro *N* provides (not a literal pinned to a different version).
2. **Given** the base image advances to version *N+1*, **When** the install runs, **Then** it resolves to *N+1*'s server package without a brittle hard-coded version breaking it.

---

### User Story 3 - Generalizes across known service kinds (Priority: P3)

The fix is install **completeness** for every coordinare-known service kind, not a one-off patch for one database — any kind whose mapping is a meta-package resolves to its concrete server package.

**Why this priority**: Prevents the same class of bug for the next declared service kind. Lower priority — postgres is the live case — but the mechanism should be general.

**Independent Test**: For each known service kind, verify its mapped package set yields runnable binaries after install (a kind mapped to a meta-package resolves the concrete server package).

**Acceptance Scenarios**:

1. **Given** any coordinare-known service kind, **When** its service-binary install runs, **Then** runnable binaries for that service are present in the cache.

---

### Edge Cases

- **Mapped package is a meta-package** (no binaries): its concrete server dependency is resolved + fetched (the core fix).
- **Mapped package already concrete** (ships binaries directly): unchanged — fetched as-is.
- **Distro version advances**: resolution follows the current distro's server package; no brittle pin breaks it.
- **Server package unavailable** in the repo for the arch/distro: the install fails clearly (and spec-101's readiness gate then blocks the bootstrap with a clear cause) — never a silent "client-only" install masquerading as complete.
- **Client still needed**: the client package continues to be installed alongside the server (both are required for tests + tooling).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: For a declared service of a coordinare-known kind, the env-bootstrap MUST install the package(s) containing the service's runnable **server** binaries (the daemon + init/teardown tools), not only a meta-package or only the client.
- **FR-002**: When a kind's mapped package is a meta/virtual (dependency-only) package, the install MUST resolve and fetch the concrete server package(s) it depends on (the full set needed to run the service), so the binaries land in the cache and are placed on the cache's path like any other cached toolchain.
- **FR-003**: After the install, the service's key server binaries MUST be present and runnable in the activated cache (the condition spec-101's readiness gate verifies); a correctly-declared service's bootstrap MUST therefore be able to pass the readiness gate.
- **FR-004**: Server-package resolution MUST be version-resilient — it MUST NOT depend on a brittle hard-coded distro/package version that breaks when the base image's distro advances; it resolves the concrete server package the current distro provides (any pin must be derived/validated, not a fragile literal).
- **FR-005**: The fix MUST apply generally to coordinare-known service kinds (postgres now; others later) — install completeness, not a postgres-only special case.
- **FR-006**: A server package that cannot be obtained MUST fail clearly (feeding the spec-101 readiness gate's block) — never a silent client-only/meta-only install treated as complete.
- **FR-007**: No secret values in logs/records; no new external dependency (reuse the existing package-download + extraction path); the single-host single-process snapshot state model is unchanged; the service-kind→package mapping + service declarations remain config/code surfaces; the base performer image stays service-agnostic (binaries ride the cache).

### Key Entities *(include if feature involves data)*

- **Service kind → package set**: the coordinare's mapping from a declared service kind to the system package(s) to install — must name (or resolve to) the package(s) carrying the runnable server binaries, plus the client.
- **Server package resolution**: turning a (possibly meta) package name into the concrete package(s) that actually contain the binaries, for the current distro/arch — the unit of work this feature adds.
- **Cached service binaries**: the runnable server binaries delivered into the env-cache (on the cache's path), consumed by the service start/health scripts + the spec-101 readiness gate.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After bootstrapping a symphony that declares a database service, the service's key server binaries are present + runnable in the cache in 100% of cases (0 caches with a "meta-package only / client only / no server binary" install).
- **SC-002**: A correctly-declared service passes the spec-101 readiness gate (starts + connectable) — the website-class failure (declared service, zero server binaries) cannot recur.
- **SC-003**: The resolution survives a base-image distro version change without a code edit hard-pinned to the old version (no brittle-pin regression).
- **SC-004**: The fix is general — every coordinare-known service kind yields runnable binaries; 0 kinds left mapped to a binary-free meta-package.
- **SC-005**: An unobtainable server package fails clearly (0 silent client-only installs marked complete); records remain secret-free.

## Assumptions

- The coordinare already has a service-kind→package mapping and a package-download path that fetches packages into the cache (091); this feature corrects what that mapping/download delivers (the server, not just the meta/client), reusing the same delivery + extraction.
- The package manager can resolve a meta-package's dependencies to the concrete server package (standard dependency resolution); the fix uses that capability rather than enumerating versions by hand.
- The base performer image is the Debian-family environment the current install path targets; non-Debian package managers are out of scope.
- Spec-101's readiness gate is in place to verify the result and block on failure; this feature makes the install it checks actually succeed for a correctly-declared service.
- Each coordinare-known kind has identifiable "key server binaries" whose presence indicates a runnable install.

## Dependencies

- Spec 091 (stateful-service hosting: the service-kind→package mapping + the deb/package delivery into the cache) — the mapping + download this corrects.
- Spec 101 (service-readiness completion gate) — verifies the binaries are present/connectable and blocks the bootstrap if not; this feature makes that gate pass for a correctly-declared service.
- The env-cache extraction + activate.sh path-placement (existing) — how cached binaries are made runnable.

## Out of Scope

- The spec-101 readiness gate itself (already shipped) — this feature makes the install it checks succeed.
- Choosing which services a symphony declares (operator/manifest config).
- Non-Debian/apt package managers (the performer base is Debian-family).
- Installing arbitrary user software beyond declared coordinare-known service kinds.
- The coordinare-side normalizer/shim work (specs 098/100).
- Changing the service start/health script templater (091) beyond what's needed for the installed binaries to be found.
