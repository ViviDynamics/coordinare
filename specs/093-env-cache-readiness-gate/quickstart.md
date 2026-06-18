# Quickstart: Env-Cache Toolchain-Readiness Dispatch Gate

Integration scenarios derived from the three user stories. Each is independently testable and maps
to the acceptance criteria in [spec.md](spec.md). Run unit tests with `.venv/bin/pytest` and lint
with `.venv/bin/ruff check`.

## Prerequisites

- A symphony with an env manifest declaring at least one pinned `runtime`, one `gem`/native
  extension, and one coordinare-managed service (postgres or redis).
- The existing `_verify_env_cache_clean` seam (`daemon.py` ~1738-1812) and dispatch readiness guard
  (`dispatch_performer.py` ~973-1000).

## Scenario US1 (P1) — Gate dispatch on real toolchain readiness

**Goal**: a code-running stage is NOT dispatched against a cache whose declared toolchain for the
current spec sha is not yet present/usable.

1. Build an env-cache where `bootstrap_complete` has fired (`last_bootstrap_succeeded == True`) but
   the ruby runtime for the current spec sha is **missing or wrong-version** (the live website
   race: dispatched 14:19 against a cache whose ruby wasn't built until 16:09).
2. Trigger a `qa` dispatch.
3. **Expect**: `_verify_env_cache_clean` returns `False`; dispatch is withheld and the slot
   released under `dispatch_performer.env_cache_not_current` → `bootstrap_in_flight`; a re-bootstrap
   is triggered for the current spec sha.
4. After bootstrap installs the correct runtime, re-dispatch: `verify.sh` passes (`True`), dispatch
   proceeds.

**Independent test**: unit-test the gate decision in `dispatch_performer` for each code-running
stage in `ROLE_TO_STAGE` — `False` ⇒ withhold+trigger, `True` ⇒ proceed, and `env_bootstrap`
stays exempt.

## Scenario US2 (P2) — Manifest-driven checklist semantics

**Goal**: `verify.sh` distinguishes installed from running/usable, per manifest item.

1. Render `verify.sh` from a manifest with a pinned runtime, a native extension (rails/psych), a
   coordinare-managed postgres service, and a qa-only nicety (chromium).
2. Assert the rendered lines:
   - runtime present + version match ⇒ `OK`; wrong version ⇒ `FAIL`.
   - native extension loads under the project Gemfile ⇒ `OK`; `require` raises ⇒ `FAIL`.
   - **postgres installed but not running ⇒ `FAIL`** (via `pg_isready`); running+healthy ⇒ `OK`.
   - chromium absent ⇒ `WARN` (non-blocking), never affecting exit code.
3. Assert aggregate exit code is nonzero iff ≥1 `FAIL:` line, else 0.

**Independent test**: `render_verify_sh` output assertions in the services test suite — line tokens
per kind, the net-new coordinare-managed-service health line at FAIL level, and exit-code aggregation.
Verify no secret values appear in any line and all interpolated tokens are `shq`-quoted.

## Scenario US3 (P3) — Self-heal, bounded by attempt budget

**Goal**: a `False` readiness self-heals via re-bootstrap and surfaces an actionable verdict when
genuinely broken, without thrashing.

1. Configure a manifest whose service can never become healthy (genuinely broken env).
2. Dispatch a code-running stage; each dispatch returns `False` and triggers re-bootstrap.
3. **Expect**: attempts increment via `EnvCacheService.check_and_trigger`; at
   `>= env_bootstrap_max_attempts` (default 3) the existing `env_cache.bootstrap_exhausted` path
   fires and the card surfaces an env-blocked verdict (consistent with spec-088 wording).
4. Change the spec sha: the budget resets and a fresh attempt cycle begins.

**Independent test**: drive the FAIL→re-bootstrap→exhaustion loop and assert (a) no new counter is
introduced (reuses `bootstrap_attempts`), (b) exhaustion at `>= max`, (c) spec-sha change resets.

## Regression guard (must stay green)

- `test_daemon_snapshot_persistence.py`: `_verify_env_cache_clean` tri-state contract — `None` when
  `verify.sh` absent (MUST NOT block), `True` on exit 0, `False`+detail on nonzero — unchanged.
- Spec-088 `monitor_performer.qa_env_blocked` integrity gate continues to fire on hollow passes.
