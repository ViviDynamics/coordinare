# Feature Specification: Service-Deb Fetch Captures the Full Runtime-Lib Closure Independent of Container State

**Feature Branch**: `106-service-deb-full-closure`
**Created**: 2026-06-22
**Status**: Draft
**Continues / fixes**: 102 (closure-resolving fetch), 103 (server-bin on PATH), 105 (host aliasing)

## Overview

Spec 102 fetches a declared service's server `.deb` via `apt-get install --download-only`. This resolves the meta→versioned-server correctly, but it is **container-install-state dependent**: `--download-only install` does not download a dependency that is already *installed* in the container. During env-bootstrap the agent installs other things first (the QA browser, build deps), which install runtime libraries (e.g. `libicu76`) into the container. By the time the service install runs, those libs are "satisfied", so `--download-only` skips them — they never land in the persistent cache. The ephemeral container is then discarded, so at QA time the postgres server cannot load `libicui18n.so.76`/`libicuuc.so.76` and fails to start. The spec-101 gate correctly reports `pg_isready` failure, but the env is unusable.

This feature makes the service-deb fetch resolve against the **pristine base image's** installed set, so it downloads exactly the packages the *base image* lacks (the service's runtime libs like `libicu76`) — regardless of what the bootstrap container has since installed — while still skipping the base OS libraries (`libc6`, `libssl`, …) that already ship in the image (so they are not wastefully re-fetched or harmfully overlaid onto `LD_LIBRARY_PATH`).

## Why

Confirmed live (2026-06-22): with services declared (score.json), 102 fetched `postgresql-17` and 103 put `initdb`/`postgres` on PATH, but postgres failed to start — `error while loading shared libraries: libicui18n.so.76`. Empirically, `apt-get install --download-only postgresql` fetches `libicu76` in a clean container but NOT when `libicu76` is already installed; resolving against a snapshot of the pristine base dpkg status fetches it either way (63 targeted packages incl `libicu76`, excl `libc6`).

## User Scenarios & Testing

### User Story 1 — service runtime libs land in the cache regardless of container state (Priority: P1) 🎯 MVP

**Acceptance**:
1. **Given** a declared postgres service and a bootstrap container that has already installed `libicu76` (e.g. via the QA browser), **when** the service-deb fetch runs, **then** `libicu76` is still downloaded into `<cache>/debs/` (resolved against the pristine base state), so the extracted postgres server can load its ICU libraries and start.

### User Story 2 — base OS libraries are not re-fetched/overlaid (Priority: P2)

**Acceptance**:
1. **Given** the base image already ships `libc6`/`libssl3`/etc., **when** the service-deb fetch runs, **then** those are NOT downloaded (the fetch resolves against the base state which already has them) — keeping the fetch targeted (~tens of packages, not the entire OS closure) and avoiding overlaying base system libraries onto `LD_LIBRARY_PATH`.

### User Story 3 — graceful fallback on older images (Priority: P3)

**Acceptance**:
1. **Given** an image built before this change (no base-state snapshot present), **when** the rendered fetch runs, **then** it falls back to the live dpkg status (the prior 102 behavior) rather than erroring.

### Edge Cases

- No declared services → no service-install block (unchanged).
- Snapshot file missing → fall back to `/var/lib/dpkg/status` (US3).
- A service runtime lib that IS in the base image → correctly not re-fetched (US2).

## Requirements

### Functional Requirements

- **FR-001**: The performer image MUST ship a snapshot of its pristine dpkg installed-state (taken at image build, after the image's own apt installs) at a well-known path, so a later resolution can target "what the base image lacks."
- **FR-002**: The rendered service-deb fetch MUST resolve the download set against that base-state snapshot (`apt-get install --download-only -o Dir::State::status=<snapshot>`), so it downloads the service's full runtime-library closure (e.g. `libicu76`) even when those libs were already installed into the bootstrap container by an earlier step.
- **FR-003**: The fetch MUST still skip packages present in the base image (e.g. `libc6`), keeping it targeted and avoiding harmful base-library overlay on `LD_LIBRARY_PATH`.
- **FR-004**: The fetch MUST retain 102's behavior otherwise: meta→versioned-server resolution via apt, `-o Dir::Cache::archives=<cache>/debs/`, version-resilient (no pinned version), and `dpkg-deb -x` extraction into a concrete dir.
- **FR-005**: If the base-state snapshot is absent (older image), the fetch MUST fall back to the live dpkg status (prior 102 behavior) — never error on the missing file.
- **FR-006**: No secret values in the instruction/logs; no new external dependency (apt + dpkg-deb only); the base image stays service-agnostic (the snapshot is a small status file; binaries still ride the cache).

### Key Entities

- **base-state dpkg snapshot**: a copy of `/var/lib/dpkg/status` captured at image build; the reference installed-set for service-deb resolution.
- **`_render_system_services_install`**: the coordinare-side persona renderer; emits the fetch command (now `-o Dir::State::status=<snapshot>`-aware with fallback).

## Success Criteria

- **SC-001**: A bootstrap whose container already has `libicu76` installed still lands `libicu76` in `<cache>/debs/`; the extracted postgres server runs (`postgres --version` succeeds) and the spec-101 gate passes (`pg_isready`). (Replays the live failure.)
- **SC-002**: The fetch does not download `libc6` (base OS lib), and the resolved set is the targeted service closure (tens of packages), not the full OS closure (~hundreds).
- **SC-003**: With the snapshot file absent, the rendered command falls back to the live status path and still runs.

## Assumptions

- The performer image is Debian-family (apt + dpkg); the build can snapshot `/var/lib/dpkg/status`.
- Capturing the snapshot in `Dockerfile.base` (the immutable OS + base-deps layer) is sufficient: it captures the OS libs we must avoid re-fetching; service runtime libs absent from base (e.g. `libicu76`) are correctly fetched even if `:full`/`:extra` later install them.

## Out of Scope

- The 101 readiness gate / 103 PATH / 105 host aliasing / 104 inference (merged; this only fixes the fetch completeness).
- Non-apt package managers; pinning service versions.
- Reworking how `*.so` are surfaced on `LD_LIBRARY_PATH` (the coordinare profile, unchanged) — this only ensures the needed `.deb`s are present to extract.
