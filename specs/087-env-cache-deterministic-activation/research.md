# Research: Deterministic Env-Cache Native-Library Activation

## Motivating evidence (empirically proven on a live QA container)

On a live `coordinare-performer:full` container with the website cache mounted:

- `bundle exec ruby -e 'require "psych"'` →
  `libyaml-0.so.2: cannot open shared object file: No such file or directory`.
- The cache's `debs/` directory **did** contain `libyaml-0-2_*.deb` / `libyaml-dev_*.deb`
  (the bootstrap captured them).
- `dpkg-deb -x <cache>/debs/libyaml-0-2_*.deb /tmp/x` followed by
  `export LD_LIBRARY_PATH=/tmp/x/usr/lib/aarch64-linux-gnu` →
  `require "psych"` then succeeds (`psych 5.3.1`).
- The LLM-authored `activate.sh` in the cache contains **no** dpkg/apt/`LD_LIBRARY_PATH`
  logic — confirming the gap is the LLM omission, not the capture step.
- `sh -c` (dash, non-interactive) does not source the activation chain; `bash -c` does
  (the dash-`$ENV`-interactive-only vs bash-`$BASH_ENV`-non-interactive asymmetry).

Conclusion: the captured debs are present and sufficient; the missing step is
deterministic extraction + `LD_LIBRARY_PATH`. That step must be coordinare-owned per the
"Guardrails for Forgetful Models" principle.

---

## Decision 1 — Extraction tool: `dpkg-deb -x`

- **Decision**: Use `dpkg-deb -x <deb> <dir>` to extract each `.deb` filesystem tree, then
  collect/expose its shared objects.
- **Rationale**: `dpkg-deb` is already in the Debian-based image, requires no root for
  `-x`, does not touch the system dpkg database (unlike `dpkg -i`), and works against a
  read-only source. `-x` is a pure unpack — no maintainer scripts, no apt, no network.
- **Refinement (implementation)**: Prefer `dpkg-deb -x` when present, but fall back to
  `ar p <deb> data.tar.* | tar -x` when `dpkg-deb` is absent. A `.deb` is an `ar` archive
  whose `data.tar.{gz,xz,zst}` member holds the filesystem tree, so `ar`+`tar` extracts the
  same `.so` files. This (a) keeps the feature working on minimal images that ship `ar`/`tar`
  but not `dpkg-deb`, and (b) makes the shell tests runnable on the macOS dev host (which has
  `ar`/`tar`/`dash` but no `dpkg-deb`), satisfying the TDD "watch it fail locally" requirement.
- **Alternatives considered**:
  - `dpkg -i` / `apt-get install`: mutates the system, needs root, can fail on deps, and is
    exactly what the LLM was (unreliably) asked to do. Rejected.
  - `ar x` + `tar` only (no dpkg-deb): viable but `dpkg-deb -x` is the canonical tool and
    handles all data.tar compressions uniformly — keep it as the preferred path, `ar`+`tar`
    as the portable fallback.

## Decision 2 — Writable lib dir location (cache mount is read-only)

- **Decision**: Extract into a container-writable base directory, namespaced per cache,
  e.g. `${_DEVENV_LIB_BASE:-/var/lib/devenv}/<slug>/lib`. The base is overridable via the
  `_DEVENV_LIB_BASE` environment variable (defaulting to a fixed image path) so tests can
  point it at a tmpdir.
- **Rationale**: `/devenv/<slug>/` is mounted read-only, so we cannot write the extracted
  `.so` files there. A per-cache namespace under a writable base avoids collisions between
  caches and keeps cleanup simple (whole container layer is ephemeral). The env override is
  the minimal seam needed for hermetic shell tests (mirrors how the existing tests already
  rewrite the `/devenv/*` glob).
- **Alternatives considered**:
  - Write into the cache: impossible (read-only). Rejected.
  - `tmpfs`/`/tmp`: lost between unrelated shells and noisier to namespace; `/var/lib/devenv`
    is a stable, writable, conventional location. Rejected in favor of a named base.

## Decision 3 — One-time extraction + sentinel

- **Decision**: Guard extraction with a per-cache sentinel file (e.g.
  `<lib_base>/<slug>/.extracted`). If present, skip `dpkg-deb` entirely and only ensure the
  lib dir is on `LD_LIBRARY_PATH`. Publish atomically: extract into a temp dir, then
  `mv` into place and create the sentinel, so a partially-extracted dir is never observed
  as complete.
- **Rationale**: `dpkg-deb` per shell would add measurable latency to every command
  (the profile is sourced on every shell). The sentinel keeps steady-state cost to a
  path-export. Atomic publish + sentinel-last handles the concurrency edge case
  (FR-009): a racing shell either sees no sentinel (re-does work into its own temp dir,
  harmless) or sees a complete published dir.
- **Alternatives considered**:
  - `flock`: stronger mutual exclusion but adds a dependency on `flock` being present and
    complicates the "never block the shell" rule. Atomic rename + idempotent re-extract is
    simpler and sufficient because extraction is deterministic and the publish is atomic.
    Rejected for v1; can be added if races prove costly.
  - No sentinel (extract every shell): violates the performance budget. Rejected.

## Decision 4 — Ordering relative to activate.sh

- **Decision**: Perform deterministic extraction + `LD_LIBRARY_PATH` export for a cache
  **before** sourcing that cache's `activate.sh`, inside the existing
  `for _devenv_activate in /devenv/*/activate.sh` loop (or a sibling loop run first).
- **Rationale**: FR-004 — native libs must be loadable irrespective of `activate.sh`
  contents. Doing it before means even an empty/buggy `activate.sh` yields a working linker
  path. If a future `activate.sh` also sets `LD_LIBRARY_PATH`, prepending ours first and
  letting theirs append is well-formed (FR-002 scenario 2).
- **Alternatives considered**: After `activate.sh` — would let a buggy script shadow or
  clobber our path and reintroduces LLM dependence. Rejected.

## Decision 5 — Safety under "sourced into every shell"

- **Decision**: All new logic uses POSIX-sh constructs only, wraps fallible operations so a
  failure for one deb/cache is logged-and-skipped (`|| continue` / guarded conditionals),
  never calls `exit`, never `return`s non-zero to the caller, and never enables
  `set -e`/`set -u`. The `_DEVENV_SOURCED` re-entry guard is preserved and the new work
  sits inside it (runs once per process tree).
- **Rationale**: FR-005/FR-006 and the documented history that a sourced `exit 1`
  deadlocked bootstraps. POSIX-only keeps dash (`sh -c`) working (FR-007).
- **Alternatives considered**: bash-only features (arrays, `[[ ]]`) — would break dash
  sourcing. Rejected.

## Decision 6 — `sh -c` (dash) reachability

- **Decision**: Rely on the image's existing `ENV=/etc/devenv-activate.sh` for dash. The
  known limitation is that dash reads `$ENV` only for *interactive* shells, so
  non-interactive `sh -c` does not auto-source. Confirm current behavior with a test; if
  `sh -c` activation is required and `$ENV` does not deliver it, document that the reliable
  cross-shell guarantee comes from the exported `PATH`/`LD_LIBRARY_PATH` (set by the
  top-level bash shell and inherited by child `sh -c` processes), and assert that
  inheritance path in tests.
- **Rationale**: The active failure (Defect A) is fixed purely by the extraction +
  `LD_LIBRARY_PATH` work, independent of shell mode. Defect B (P2) is about closing the
  asymmetry; the practical guarantee is environment inheritance from the activated parent
  shell, which the tests pin down. This avoids over-engineering a dash-interactive-only
  workaround.
- **Alternatives considered**: Forcing `ENV` sourcing for non-interactive dash via wrapper
  shims — invasive and brittle. Deferred unless tests show inheritance is insufficient.

## Open questions

None. All `NEEDS CLARIFICATION` resolved. Fix approach (deterministic image layer) and
process (full speckit spec) were explicitly chosen by the user.
