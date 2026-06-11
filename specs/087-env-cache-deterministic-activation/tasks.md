# Tasks: Deterministic Env-Cache Native-Library Activation

**Input**: Design documents from `/specs/087-env-cache-deterministic-activation/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/devenv-profile.contract.md

**Tests**: REQUIRED. This feature is TDD (user-mandated Iron Law). Every behavioral change
to `agent/performer/devenv-profile.sh` is preceded by a failing shell-level test in
`tests/unit/test_devenv_profile_shell.py`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files / independent, no incomplete deps)
- **[Story]**: US1 (P1 native-lib loadable), US2 (P1 LLM-independent), US3 (P2 sh -c)

## Path Conventions

- Production asset: `agent/performer/devenv-profile.sh`
- Tests: `tests/unit/test_devenv_profile_shell.py`
- Run tests: `.venv/bin/pytest tests/unit/test_devenv_profile_shell.py -v`
- Lint: `.venv/bin/ruff check tests/unit/test_devenv_profile_shell.py`

---

## Phase 1: Setup (Shared Infrastructure)

- [X] T001 Add a pytest helper to `tests/unit/test_devenv_profile_shell.py` that builds a
  synthetic `.deb` at test time using `dpkg-deb -b` (or `ar`) containing a dummy shared
  object (e.g. `usr/lib/<triplet>/libfake.so.1`), plus a fixture that constructs a fake
  cache tree `<tmp>/devenv/<slug>/{debs/<the deb>, activate.sh}`. Skip cleanly if
  `dpkg-deb`/`ar` is unavailable (mirror the existing `shutil.which("bash")` skip).
- [X] T002 [P] Extend `_patched_profile` (or add a sibling helper) in
  `tests/unit/test_devenv_profile_shell.py` so tests can point the writable lib base at a
  tmpdir via the `_DEVENV_LIB_BASE` env var, alongside the existing `/devenv/*` glob rewrite.

**Checkpoint**: Test harness can synthesize debs, fake caches, and redirect the lib base.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ No user-story implementation may begin until the test harness (Phase 1) exists, since
every implementation task is gated on a failing test that uses it.**

- [X] T003 Confirm (read-only) that `dpkg-deb` is present in the performer image by
  inspecting `agent/performer/Dockerfile.base` / `Dockerfile.full`; record the finding in
  research.md if it requires an explicit package install. (No code unless missing.)

**Checkpoint**: Extraction tool availability confirmed; harness ready.

---

## Phase 3: User Story 1 — Native libs loadable from captured debs (Priority: P1) 🎯 MVP

**Goal**: For a cache with `debs/*.deb`, extract `.so` files to a writable per-cache lib
dir and prepend it to `LD_LIBRARY_PATH` (one-time, sentinel-guarded), so a runtime linked
against those libs loads.

**Independent Test**: Source the patched profile against a fake cache whose deb carries
`libfake.so.1`; assert the `.so` lands under `<lib_base>/<slug>/lib` and the dir is on
`LD_LIBRARY_PATH`.

### Tests first (write, watch fail)

- [X] T004 [US1] Write failing test `test_profile_extracts_debs_and_sets_ld_library_path`
  in `tests/unit/test_devenv_profile_shell.py`: given a fake cache with a synthetic deb,
  after sourcing the patched profile, assert the extracted `.so` exists under the
  redirected lib base and that dir appears in `LD_LIBRARY_PATH` (Contract C1). Run and
  confirm it FAILS for the right reason.
- [X] T005 [P] [US1] Write failing test
  `test_profile_skips_extraction_when_sentinel_present`: pre-create the sentinel + lib dir,
  source the profile, assert `dpkg-deb` is NOT re-run (e.g. detect via a shimmed `dpkg-deb`
  on PATH that writes a marker, or via mtime) yet the lib dir is still on
  `LD_LIBRARY_PATH` (Contract C3). Confirm it FAILS.
- [X] T006 [P] [US1] Write failing test
  `test_profile_no_debs_dir_leaves_ld_library_path_unchanged`: fake cache with no `debs/`;
  assert clean exit and no lib-dir entry added for that cache (Contract C4). Confirm it
  FAILS (or errors) against current script.
- [X] T007 [P] [US1] Write failing test `test_profile_preserves_existing_ld_library_path`:
  set a pre-existing `LD_LIBRARY_PATH`, source profile, assert prior value is retained and
  the new dir is prepended. Confirm it FAILS.

### Implementation (make green, minimal)

- [X] T008 [US1] In `agent/performer/devenv-profile.sh`, add a POSIX-sh function that, for a
  given cache dir, computes `<lib_base>/<slug>` from `${_DEVENV_LIB_BASE:-/var/lib/devenv}`,
  and if `<cache>/debs` has `*.deb` and no `.extracted` sentinel, extracts each deb with
  `dpkg-deb -x` into a temp dir, collects `*.so*` into `<lib_base>/<slug>/lib`, atomically
  publishes, then writes the sentinel. Always prepend `<lib_base>/<slug>/lib` to
  `LD_LIBRARY_PATH` when it exists. Make T004–T007 pass.
- [X] T009 [US1] Run `.venv/bin/pytest tests/unit/test_devenv_profile_shell.py -v`; confirm
  all US1 tests green and the pre-existing profile tests still pass. Mark T004–T008 done.

**Checkpoint**: US1 delivers the active-failure fix (native libs loadable). MVP complete.

---

## Phase 4: User Story 2 — Activation independent of activate.sh (Priority: P1)

**Goal**: Native-lib activation happens before `activate.sh` and does not depend on its
contents.

**Independent Test**: Use today's verbatim no-deb-handling `activate.sh`; assert the lib
dir is on `LD_LIBRARY_PATH` at the moment `activate.sh` is sourced.

### Tests first

- [X] T010 [US2] Write failing test
  `test_ld_library_path_set_before_activate_sh_sourced`: make `activate.sh` echo a sentinel
  capturing `$LD_LIBRARY_PATH` (e.g. to a file) when sourced; assert the lib dir is already
  present in that captured value (ordering, Contract C2/C4-before). Confirm it FAILS.
- [X] T011 [P] [US2] Write failing test
  `test_activate_sh_appending_ld_library_path_is_well_formed`: an `activate.sh` that also
  appends to `LD_LIBRARY_PATH`; assert the final value contains both our lib dir and theirs
  and is not malformed (Contract C7). Confirm it FAILS.

### Implementation

- [X] T012 [US2] In `agent/performer/devenv-profile.sh`, ensure the extraction/export step
  for each cache runs BEFORE that cache's `activate.sh` is sourced (order the loop or run a
  dedicated pre-pass). Make T010–T011 pass without breaking US1 tests.
- [X] T013 [US2] Re-run the full shell test file; confirm green. Mark T010–T012 done.

**Checkpoint**: LLM removed from the native-lib critical path.

---

## Phase 5: User Story 3 — `sh -c` (dash) gets activated env (Priority: P2)

**Goal**: Non-interactive `sh -c` observes the same `PATH`/`LD_LIBRARY_PATH` as `bash -c`.

**Independent Test**: Run a command via `sh -c` and via `bash -c`; assert identical
activated `PATH`/`LD_LIBRARY_PATH` (via direct sourcing or inheritance from the activated
parent).

### Tests first

- [X] T014 [US3] Write failing/【characterization】test
  `test_dash_sh_c_sees_activated_environment`: source the profile under `dash`/`sh` and
  assert the new logic runs without dash-incompatible syntax errors and the lib dir is on
  `LD_LIBRARY_PATH` (Contract C8). Confirm it FAILS or errors on any bashism.
- [X] T015 [P] [US3] Write test
  `test_sh_c_inherits_activated_env_from_parent`: export an activated env from a parent
  bash that sourced the profile, then invoke `sh -c` and assert inheritance of
  `PATH`/`LD_LIBRARY_PATH` (pins the practical cross-shell guarantee). Confirm it FAILS if
  not yet satisfied.

### Implementation

- [X] T016 [US3] Ensure all new lines in `agent/performer/devenv-profile.sh` are POSIX-sh
  only (no `[[ ]]`, arrays, `local` if unsupported, `mapfile`, etc.) so dash sources it
  cleanly; adjust to satisfy T014–T015. Make tests green.
- [X] T017 [US3] Re-run full shell test file under both bash and dash paths; confirm green.
  Mark T014–T016 done.

**Checkpoint**: Shell-mode asymmetry closed / documented and tested.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T018 [P] Add safety + concurrency regression tests in
  `tests/unit/test_devenv_profile_shell.py`:
  `test_profile_never_aborts_on_corrupt_deb` (Contract C5),
  `test_profile_does_not_enable_set_e_or_exit` (Contract C9 — assert sourcing a profile
  followed by a deliberately failing command still continues; grep the script for forbidden
  `set -e`/`set -u`/`exit`), and `test_profile_partial_lib_dir_without_sentinel_reextracts`
  (FR-009 — pre-create a lib dir with NO `.extracted` sentinel; assert sourcing re-runs
  extraction and publishes the sentinel, so a partial/interrupted publish is never treated
  as complete). Then make green.
- [X] T019 [P] Verify FR-010: read `src/coordinare/services/http_performer_service.py`
  `_serialize_env_bootstrap` (deb-capture into `{cache_mount_path}/debs/`) and confirm the
  capture instruction is intact; if the bootstrap previously asked `activate.sh` to install
  debs, trim that now-redundant instruction (and update its unit test in
  `tests/unit/test_060_env_cache.py`) so guidance matches the new deterministic owner.
- [X] T020 Update `agent/performer/devenv-profile.sh` header comment to document the new
  deterministic deb-extraction behavior, the `_DEVENV_LIB_BASE` override, and the
  never-`exit`/never-`set -e` constraint.
- [X] T021 Run full repo unit suite `.venv/bin/pytest tests/unit -q` and
  `.venv/bin/ruff check` on touched files; confirm no regressions and clean lint.
- [X] T022 Update `specs/087-env-cache-deterministic-activation/quickstart.md` if the
  final lib-base path or sentinel name differs from the planned defaults.

---

## Dependencies & Execution Order

- **Phase 1 (T001–T002)** → unblocks all test-first tasks.
- **Phase 2 (T003)** → confirms tool availability.
- **US1 (T004–T009)** → MVP; must complete before US2 ordering work is meaningful.
- **US2 (T010–T013)** → depends on US1 extraction existing.
- **US3 (T014–T017)** → depends on the new lines existing (US1/US2) to assert dash-safety.
- **Polish (T018–T022)** → after US1–US3 green.

## Parallel Opportunities

- T005, T006, T007 (independent test cases, same file — coordinate edits but logically
  parallel authoring).
- T011 parallel to T010 authoring.
- T018 and T019 are independent (different files).

## MVP Scope

**US1 alone** (T001–T009) is a viable MVP: it fixes the active production failure (Ruby QA
dying on `libyaml-0.so.2`). US2 and US3 harden architecture and close the shell asymmetry.
