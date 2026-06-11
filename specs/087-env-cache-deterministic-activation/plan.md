# Implementation Plan: Deterministic Env-Cache Native-Library Activation

**Branch**: `087-env-cache-deterministic-activation` | **Date**: 2026-06-10 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/087-env-cache-deterministic-activation/spec.md`

## Summary

Move native-library activation for env caches out of the LLM-authored `activate.sh`
and into the coordinare-owned, image-baked profile script (`agent/performer/devenv-profile.sh`).
For each mounted cache `/devenv/*/` containing a `debs/` directory, the profile script
deterministically extracts the shared objects from those `.deb` packages to a
**writable** per-cache lib directory (outside the read-only mount), prepends that
directory to `LD_LIBRARY_PATH`, and does so **before** sourcing `activate.sh` — removing
the LLM from the critical correctness path. Extraction is one-time (sentinel-guarded) and
concurrency-safe; the script remains safe to source into every shell (no `exit`, no
non-zero `return`, no `set -e`/`set -u`). The activation chain is also confirmed reachable
for non-interactive `sh -c` (dash) invocations.

## Technical Context

**Language/Version**: POSIX shell (sourced by both bash and dash) for the profile script;
Python 3.14 (project min 3.12) for any coordinare-side serialization changes and the test
harness.
**Primary Dependencies**: `dpkg-deb` (already present in the Debian-based performer image)
for extracting `.deb` archives; no new runtime dependency. pytest + subprocess for shell tests.
**Storage**: N/A — no persisted coordinare state. The writable per-cache lib dir and the
extraction sentinel live on the container's ephemeral writable layer, not in any store.
**Testing**: pytest (`.venv/bin/pytest`) driving the shell script via `subprocess` across
bash login / bash `-c` (`BASH_ENV`) / dash `-c` (`ENV`) modes, mirroring the existing
`tests/unit/test_devenv_profile_shell.py` harness.
**Target Platform**: Linux (Debian-based `coordinare-performer:full` image, arm64/amd64).
**Project Type**: Single project — coordinare orchestrator + baked performer image asset.
**Performance Goals**: One-time deb extraction per cache per container; subsequent shells
add only a path-export (sub-millisecond). Profile sourcing overhead for the common
already-extracted case MUST stay negligible (no per-shell `dpkg-deb` invocation).
**Constraints**:
- Profile script is sourced into **every** shell → MUST NOT `exit`, MUST NOT `return`
  non-zero to the sourcing shell, MUST NOT enable `set -e`/`set -u`.
- Cache mount is **read-only** → extraction target MUST be writable and outside `/devenv/<slug>/`.
- Must work under both bash (`BASH_ENV`) and dash (`ENV`) and preserve the `_DEVENV_SOURCED`
  re-entry guard.
- Extraction must be concurrency-safe (atomic publish / lock; partial dir not treated as done).
**Scale/Scope**: A handful of caches mounted per performer; tens of `.deb` files per cache.
Single-host, single-process orchestration (unchanged).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS. Change is a small, single-purpose addition to one
  coordinare-owned script plus possibly a serialization comment. No dead code; shell kept
  readable with explanatory comments matching the existing file's style.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS, and this feature is explicitly TDD
  (user-mandated Iron Law). New shell-level unit tests in
  `tests/unit/test_devenv_profile_shell.py` written first and watched fail. Tests are
  deterministic (fixture-built fake cache trees, fake `.deb` built at test time). Coverage
  does not regress.
- **III. User Experience Consistency**: N/A (no user-facing UI). The "user" here is the QA
  performer; the relevant consistency is that QA returns a substantive verdict instead of a
  spurious "deps missing".
- **IV. Performance by Design**: PASS. Budget defined in Technical Context (one-time
  extraction; negligible steady-state per-shell cost). SC-* in the spec are the measurable
  outcomes. No CI benchmark warranted for a one-time shell extraction; steady-state cost is
  asserted by test (no `dpkg-deb` call when sentinel present).
- **V. Clarity Before Action**: PASS. No `NEEDS CLARIFICATION` markers remain; the fix
  approach (deterministic image layer) and process (full speckit spec) were explicitly
  chosen by the user. Open design choices (writable dir location, sentinel mechanism) are
  resolved in research.md below, not left ambiguous.

**Result**: PASS. No violations; Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/087-env-cache-deterministic-activation/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (shell contract for the profile script)
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
agent/performer/
└── devenv-profile.sh           # PRIMARY CHANGE: deterministic deb extraction + LD_LIBRARY_PATH

src/coordinare/services/
├── http_performer_service.py   # _serialize_env_bootstrap — verify deb-capture instruction unchanged/aligned
└── env_manifest.py             # render_verify_sh — reference only (no required change expected)

tests/unit/
└── test_devenv_profile_shell.py   # PRIMARY TEST TARGET: new failing shell tests first
```

**Structure Decision**: Single-project layout. The behavioral change is concentrated in
the one coordinare-owned shell asset `agent/performer/devenv-profile.sh` (baked into the
performer image). Tests live alongside the existing shell-level suite in
`tests/unit/test_devenv_profile_shell.py`. Coordinare-side serialization
(`http_performer_service.py`) is reviewed to confirm the deb-capture instruction still
populates `debs/` (FR-010) but is expected to need no behavioral change.

## Complexity Tracking

> No constitution violations. Section intentionally empty.
