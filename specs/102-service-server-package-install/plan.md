# Implementation Plan: Env-Bootstrap Delivers Runnable Service Server Binaries

**Branch**: `102-service-server-package-install` | **Date**: 2026-06-22 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/102-service-server-package-install/spec.md`

## Summary

The service-binary fetch is **agent-driven** via the persona block rendered in `http_performer_service._render_system_services_install` (~L898-912). It instructs the bootstrap agent to fetch the service packages with:

```
apt-get download $(apt-cache depends --recurse --no-recommends … -i postgresql postgresql-client | grep '^\w' | sort -u)
```

That pipeline is **fragile**: the `-i` (important-only) filter + `grep '^\w'` dropped the versioned server (`postgresql-NN`), so the live cache got only the arch-independent **meta** debs (`postgresql`, `postgresql-client`) + `libpq5` — **no `initdb`/`pg_ctl`/`postgres`**. The package list itself comes from `env_manifest._SERVICE_KIND_PACKAGES` (`postgres → ("postgresql","postgresql-client")`, `redis → ("redis-server",)`) — naming the meta is fine *if* the fetch resolves its dependency closure.

The fix: replace the fragile fetch command with a **robust, version-resilient dependency-closure download** — `apt-get install -y --download-only -o Dir::Cache::archives=<cache>/debs/ <pkgs>` — which lets apt resolve the meta-package to the concrete `postgresql-NN` server (+ all deps) automatically, for whatever distro the base image currently provides (no hard-pinned version). Spec-101's readiness gate remains the backstop: if the binaries still don't materialize, the bootstrap fails loudly rather than completing a broken cache.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv); the rendered instruction is shell run in the Debian-family performer.
**Primary Dependencies**: `http_performer_service._render_system_services_install` (the persona block — the fetch command); `env_manifest._SERVICE_KIND_PACKAGES` + `derive_service_install_items` (the kind→package source of truth); the env-cache deb extraction + activate.sh path-placement; spec-101 `run_service_readiness` (the verifier/backstop). **No new external dependency** (apt is already used).
**Storage**: none new.
**Testing**: pytest — the rendered persona block uses the robust closure command (`apt-get install … --download-only … -o Dir::Cache::archives=<cache>/debs/`), names the kind's server+client packages, contains NO brittle hard-pinned version (no `postgresql-17` literal), and is empty for no-services (unchanged). Optionally an integration-style check that the command resolves a versioned server in a Debian context.
**Target Platform**: performer container (Debian-family) at env_bootstrap.
**Project Type**: single project (coordinare renders the bootstrap persona; performer executes it).
**Performance Goals**: same single download step (now resolving the full closure); no per-card cost.
**Constraints**: version-resilient (no hard-pin); secret-free; base image stays service-agnostic; no-services unchanged.
**Scale/Scope**: the service-binary fetch instruction only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. A focused fix to one rendered instruction (robust apt command) + reuse of the existing mapping; no new deps/state. Removes a fragile `apt-cache depends|grep` pipeline.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests that the rendered block uses the closure-resolving download, names server+client, has no hard-pinned version, and is empty for no-services.
- **III. User Experience Consistency** — PASS. Same deb-into-cache delivery; spec-101 gate unchanged as the backstop/operator surface.
- **IV. Performance by Design** — PASS. One download step; no per-card cost.
- **V. Clarity Before Action** — PASS. Root cause located (the fragile fetch pipeline) + the robust replacement (`apt-get install --download-only` closure) identified; version-resilience handled by letting apt resolve the current server. Resolved in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/102-service-server-package-install/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── service-server-install.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
src/coordinare/services/
├── http_performer_service.py   # _render_system_services_install: replace the fragile
│                               #   `apt-get download $(apt-cache depends --recurse …|grep)`
│                               #   with a closure-resolving `apt-get install -y
│                               #   --download-only -o Dir::Cache::archives=<cache>/debs/ <pkgs>`
│                               #   (+ extract) — version-resilient, no hard-pin.
└── env_manifest.py             # _SERVICE_KIND_PACKAGES — unchanged (meta name is fine once the
                                #   fetch resolves the closure); confirm postgres/redis sets.

tests/                          # persona-render tests (robust command, server+client, no pin,
                                #   no-services empty); reuse spec-101 as the runtime verifier.
```

**Structure Decision**: Single project — the fix is coordinare-side (the rendered bootstrap instruction). The performer executes the (now robust) command; spec-101 verifies the result.

## Phase 0 — Research

See [research.md](research.md): why the `apt-cache depends --recurse … -i | grep '^\w'` pipeline dropped the versioned server; `apt-get install --download-only -o Dir::Cache::archives` as the robust closure-resolving alternative (and why it's version-resilient — apt picks the current `postgresql-NN`); keep the meta package name in the mapping (let apt resolve) vs hard-coding the versioned server (rejected: brittle, FR-004); agent-executed-instruction vs deterministic step (keep agent-executed with the robust command + the 101 backstop; full determinism deferred as a larger change).

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (kind→package mapping + the resolved closure; no persisted state), [contracts/service-server-install.md](contracts/service-server-install.md) (the rendered-instruction contract + invariants), and [quickstart.md](quickstart.md) (the website Postgres install replayed: meta → versioned server present).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
