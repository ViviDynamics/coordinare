# Feature Specification: Deterministic Env-Cache Native-Library Activation

**Feature Branch**: `087-env-cache-deterministic-activation`  
**Created**: 2026-06-10  
**Status**: Draft  
**Input**: User description: "Deterministically activate env cache native libraries in the performer image layer rather than relying on the LLM-authored activate.sh, by extracting captured debs shared objects to a writable lib dir and exporting LD_LIBRARY_PATH before sourcing activate.sh, and hardening the sh -c activation gap"

## Context

Coordinare dispatches Docker "performers" to work board cards. An `env_bootstrap` role
installs a project's toolchain into a host-backed **env cache**; downstream performers
(implementer, QA) mount that cache **read-only** at `/devenv/<slug>/` and rely on an
activation chain to put the toolchain on `PATH`.

The activation chain is:

- `Dockerfile.full` sets `ENV BASH_ENV=/etc/devenv-activate.sh` and `ENV=/etc/devenv-activate.sh`.
- `/etc/devenv-activate.sh` → symlink → `/etc/profile.d/zz-devenv.sh`
  (the coordinare-owned `agent/performer/devenv-profile.sh`, baked into the image).
- That profile script globs `/devenv/*/activate.sh` and sources each one, guarded by a
  `_DEVENV_SOURCED` re-entry guard.
- `activate.sh` itself is **authored by the bootstrap LLM** and written into the cache.

Two defects in this chain make QA unreliable:

**Defect A — native libraries are never made loadable.** When the bootstrap compiles a
runtime (e.g. Ruby 3.4.2) on the host, that runtime is dynamically linked against shared
objects (e.g. `libyaml-0.so.2`) that are **not present in the consumer image**. The
bootstrap correctly downloads the needed `.deb` packages into the cache's `debs/`
directory, but the LLM-authored `activate.sh` does **not** extract/install them and does
**not** set `LD_LIBRARY_PATH`. As a result, loading the runtime fails at the dynamic
linker (`libyaml-0.so.2: cannot open shared object file`). For Ruby this kills `psych.so`,
which Bundler/Rails/RSpec all load — so every Ruby QA run dies, and QA honestly reports
"dependencies missing." This is the active production failure.

**Defect B — `sh -c` does not activate.** `/bin/sh` is dash. dash reads `$ENV` only for
*interactive* shells, so a non-interactive `sh -c "..."` never sources the activation
chain. bash reads `$BASH_ENV` for non-interactive shells, so `bash -c` does activate.
Today the top-level performer shell is bash (child processes inherit the exported `PATH`),
so this is latent rather than the active failure — but it is a correctness gap that makes
activation depend on which shell happens to invoke a command.

The root principle (project memory, "Guardrails for Forgetful Models"): critical
correctness must live in **durable, coordinare-owned contracts**, not in LLM-authored
scripts. Native-library availability is exactly such a correctness concern. This feature
moves native-library activation out of the LLM's `activate.sh` and into the
coordinare-owned, image-baked profile script, making it deterministic.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ruby QA runs succeed when the cache carries native-lib debs (Priority: P1)

A QA performer mounts an env cache that contains a host-compiled Ruby plus the
`debs/*.deb` packages providing Ruby's required shared objects (e.g. `libyaml`). When the
QA shell starts, the coordinare-owned profile script deterministically makes those shared
objects loadable, so `ruby`, `bundle`, and `rspec` start without dynamic-linker errors and
QA can actually exercise the project.

**Why this priority**: This is the active production failure. Without it, Ruby (and any
natively-linked toolchain) QA cannot run at all, and QA's verdict is meaningless.

**Independent Test**: Given a cache directory containing `debs/libyaml-0-2_*.deb`, when the
profile script runs in a container lacking `libyaml-0.so.2`, then `ruby -ryaml -e 'true'`
(or equivalent `require "psych"`) exits 0.

**Acceptance Scenarios**:

1. **Given** a read-only cache mount at `/devenv/<slug>/` with a `debs/` directory holding
   shared-object `.deb` packages, **When** a new shell sources the profile script,
   **Then** the `.so` files from those debs are extracted to a writable per-cache lib
   directory and that directory is prepended to `LD_LIBRARY_PATH`.
2. **Given** the extraction has already run once for a cache (sentinel present), **When**
   another shell sources the profile script, **Then** extraction is **not** repeated, but
   `LD_LIBRARY_PATH` still includes the lib directory.
3. **Given** a cache with **no** `debs/` directory, **When** the profile script runs,
   **Then** it completes successfully and changes nothing about `LD_LIBRARY_PATH` for that
   cache.

---

### User Story 2 - Activation is independent of LLM-authored activate.sh for native libs (Priority: P1)

The deterministic native-library activation happens **before** the LLM-authored
`activate.sh` is sourced and does not depend on its contents. Even if `activate.sh` omits
deb handling entirely (as today's does), native libraries are still loadable.

**Why this priority**: Removing the LLM from the critical correctness path is the core
architectural goal; it is what makes QA trustworthy run-over-run regardless of how the
bootstrap model authored its script.

**Independent Test**: Given an `activate.sh` that contains **no** dpkg/apt/`LD_LIBRARY_PATH`
logic (verbatim today's), when the profile script runs, then native libraries from `debs/`
are still loadable.

**Acceptance Scenarios**:

1. **Given** an `activate.sh` with no native-lib handling, **When** the profile script
   runs, **Then** `LD_LIBRARY_PATH` already contains the extracted lib dir at the moment
   `activate.sh` is sourced.
2. **Given** both the deterministic extraction and a (hypothetical) `activate.sh` that also
   sets `LD_LIBRARY_PATH`, **When** the profile script runs, **Then** the result is
   well-formed (no duplication-induced breakage) and both paths are present.

---

### User Story 3 - Non-interactive `sh -c` commands get an activated environment (Priority: P2)

A command run via `sh -c "..."` (dash, non-interactive) sees the activated toolchain and
native libraries, not just commands run via `bash -c`.

**Why this priority**: Closes the latent shell-asymmetry gap so activation no longer
depends on which shell an agent or sub-tool happens to spawn. Lower than P1 because the
current top-level shell is bash, so it is not the active failure — but it removes a sharp
edge.

**Independent Test**: Given the image, when a command is run as `sh -c 'echo "$LD_LIBRARY_PATH"; command -v ruby'`, then the activated lib dir and toolchain are present in the output.

**Acceptance Scenarios**:

1. **Given** a performer image with caches mounted, **When** a tool invokes
   `sh -c "<cmd>"`, **Then** `<cmd>` runs with the activated `PATH` and `LD_LIBRARY_PATH`.

---

### Edge Cases

- **Read-only cache mount**: the cache is mounted read-only, so extraction MUST target a
  writable location (not under `/devenv/<slug>/`). The writable lib dir and the
  one-time-extraction sentinel both live outside the read-only mount.
- **Sourced into every shell**: the profile script is sourced into *every* shell via
  `BASH_ENV`/`ENV`. It MUST NOT `exit`, MUST NOT `return` non-zero, and MUST NOT enable
  `set -e`/`set -u`. A failure in extraction for one cache MUST NOT abort the shell or
  prevent other caches from activating. (A sourced `exit 1` has previously deadlocked
  bootstraps.)
- **Corrupt or partial `.deb`**: a deb that fails to extract MUST be skipped without
  aborting the shell or the rest of the loop.
- **Multiple caches mounted**: each `/devenv/*/` cache with a `debs/` dir gets its own
  writable lib dir and contributes to `LD_LIBRARY_PATH`.
- **Concurrent shells racing on first extraction**: two shells starting at once MUST NOT
  corrupt the lib dir; extraction must be safe under concurrency (e.g. atomic
  publish or lock), and a partially-extracted dir must not be treated as complete.
- **No debs but native runtime present**: nothing to extract; behavior is unchanged and
  the shell still starts cleanly (the lib may simply be unavailable, same as today — out
  of scope to synthesize missing debs).
- **Re-entry guard interaction**: the existing `_DEVENV_SOURCED` re-entry guard must
  continue to prevent recursion; the new logic must live correctly relative to that guard.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare-owned, image-baked profile script (`agent/performer/devenv-profile.sh`)
  MUST, for each mounted cache `/devenv/*/` that contains a `debs/` directory,
  deterministically make the shared objects (`*.so*`) contained in those `.deb` packages
  loadable by the dynamic linker.
- **FR-002**: The profile script MUST extract those shared objects to a **writable**
  per-cache library directory that is NOT inside the read-only cache mount, and MUST
  prepend that directory to `LD_LIBRARY_PATH`.
- **FR-003**: Extraction MUST be performed at most once per cache per container lifetime,
  guarded by a sentinel; subsequent shells MUST skip extraction but MUST still ensure the
  lib directory is on `LD_LIBRARY_PATH`.
- **FR-004**: The deterministic native-lib activation MUST occur **before** the
  LLM-authored `activate.sh` for that cache is sourced, and MUST NOT depend on any content
  in `activate.sh`.
- **FR-005**: The profile script MUST remain safe to source into every shell: it MUST NOT
  call `exit`, MUST NOT `return` a non-zero status to the sourcing shell, and MUST NOT
  enable `set -e` or `set -u`. A failure handling one cache or one deb MUST NOT abort the
  shell or skip remaining caches/debs.
- **FR-006**: The existing `_DEVENV_SOURCED` re-entry guard behavior MUST be preserved (no
  infinite recursion; idempotent re-sourcing).
- **FR-007**: Native-library and toolchain activation MUST apply to non-interactive
  `sh -c` invocations (dash), not only `bash -c` — i.e. the activation chain MUST be
  reachable for the POSIX-sh non-interactive case.
- **FR-008**: Behavior MUST be unchanged for caches that have no `debs/` directory and for
  containers with no mounted caches (clean start, no `LD_LIBRARY_PATH` mutation for those
  caches).
- **FR-009**: Extraction MUST be concurrency-safe: simultaneous shells MUST NOT corrupt the
  writable lib directory, and a partially-populated directory MUST NOT be treated as a
  completed extraction.
- **FR-010**: The coordinare MUST NOT regress the existing bootstrap instruction that
  downloads the required `.deb` packages into the cache's `debs/` directory (that capture
  step remains the source of the shared objects this feature activates).

### Key Entities

- **Env cache** (`/devenv/<slug>/`): read-only mounted directory containing the
  host-built toolchain, the LLM-authored `activate.sh`, and a `debs/` directory of
  captured `.deb` packages.
- **Captured deb set** (`/devenv/<slug>/debs/*.deb`): packages providing shared objects
  the host-built toolchain links against but the consumer image lacks.
- **Writable per-cache lib dir**: a container-writable directory (outside the read-only
  mount) holding shared objects extracted from the captured debs; prepended to
  `LD_LIBRARY_PATH`.
- **Extraction sentinel**: a marker indicating extraction for a given cache has completed,
  enabling one-time deterministic extraction.
- **Profile script** (`agent/performer/devenv-profile.sh`): the coordinare-owned,
  image-baked script sourced into every shell that performs the deterministic activation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a consumer image that lacks `libyaml-0.so.2`, given a cache whose `debs/`
  contains the libyaml deb(s), `ruby -ryaml -e 'true'` (or `require "psych"`) exits 0 — up
  from a 100% dynamic-linker failure today.
- **SC-002**: Ruby-based QA runs (Bundler/RSpec) that previously aborted at
  `libyaml-0.so.2: cannot open shared object file` complete their suites and return a
  substantive QA verdict (pass/fail on tests) rather than an "environment/deps missing"
  verdict.
- **SC-003**: The same QA result holds regardless of whether the LLM-authored `activate.sh`
  contains any deb-handling logic (verified by running with today's no-deb-handling
  `activate.sh`).
- **SC-004**: A command run via `sh -c` observes the same activated `PATH` and
  `LD_LIBRARY_PATH` as the same command run via `bash -c`.
- **SC-005**: Sourcing the profile script never aborts a shell: starting a shell with a
  malformed/partial deb or an unwritable target degrades gracefully (shell still usable),
  with zero bootstrap deadlocks attributable to the profile script.
