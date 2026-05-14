# Implementation Plan: QA Cycle 062

**Branch**: `062-qa-cycle`
**Spec**: [spec.md](spec.md)

## Overview

Rolling QA cycle following 060/061. Each fix is independent and added to this plan as live testing surfaces it. Initial scope is one fix; expect more to be appended.

## Implementation Strategy

Each fix is a small, contained edit with a regression test. Fixes ship together as a single PR at end of cycle (per the 058/061 precedent), but each is self-contained and could be cherry-picked.

---

## Fix 1 — Force-bootstrap preflight for performer registration (FR-001 to FR-004)

**Files**:
- `src/coordinare/dashboard.py` — endpoint `trigger_env_bootstrap`
- `tests/unit/test_dashboard.py` — regression test + helper

**Root cause**: `trigger_env_bootstrap` only checks that the global `env_cache_service` handle is present on `daemon.state`. It never verifies that the symphony's `env_bootstrap_performer_id` is actually registered with the daemon's `performer_services_by_id` map. When the id is missing (typo in `config.yaml`, partial config reload, daemon half-init), the endpoint accepts the request with `202`. The actual dispatch then runs on the next cycle inside `_execute_bootstrap_dispatch`, which logs `env_cache.bootstrap_svc_not_found` and flips `on_bootstrap_complete(success=False)` — none of which surfaces to the operator who pressed the button.

**Approach**:

1. After the existing `env_cache_service is None` check, look up `sym_cfg.env_bootstrap_performer_id` in `daemon.state["performer_services_by_id"]` (populated at startup in `__main__._bootstrap_services`).
2. If the id is missing, return `503` with a body that names the performer and points at `config.yaml`. Include `performer_id` separately in the body so the dashboard can render it without parsing the message.
3. Leave all subsequent checks (cache state, bootstrap-in-flight) unchanged.

**Why 503 and not 400**: the configuration itself is valid (a performer id is set); the *daemon state* is currently inconsistent with that configuration. 503 matches the existing precedent for "service-side resource not ready" already used by the `env_cache_service is None` and `env_cache state not initialised` branches.

**Code site**: `src/coordinare/dashboard.py:3251` — insert new block immediately after the `env_cache_service` 503 and before the `env_cache` state lookup.

**Test approach**: extend the existing `_attach_env_cache` helper in `tests/unit/test_dashboard.py` to also seed `performer_services_by_id` so all existing 202/409/503 tests continue to pass with the stricter check. Add `test_force_env_bootstrap_missing_performer_service_returns_503` to cover the new branch.

---

## Fix 2 — RTK token compression in performer containers (FR-005 to FR-008)

**Files**:
- `agent/performer/Dockerfile.base` (or `.full`) — install pinned `rtk` binary
- `agent/performer/entrypoint.sh` — conditional `rtk init` per backend
- `config.example.yaml` — document the `RTK_ENABLED` env knob on `codex-ephemeral`
- `tests/unit/test_performer_lifecycle.py` (or equivalent) — assert env propagation when `env: {RTK_ENABLED: "1"}` is set on a `PerformerEndpointConfig`

**Root cause / motivation**: Performer Bash output (git/pytest/cargo/ls) is one of the largest input-token consumers per card. RTK is a drop-in CLI proxy with a documented auto-rewrite hook for both Claude Code and the Codex CLI that compresses these outputs 60–90% with no protocol change on coordinare's side.

**Approach**:

1. **Image build (Dockerfile)**: add an install step that pulls a pinned `rtk` release tarball from `github.com/rtk-ai/rtk/releases/download/<version>/` for `linux/<arch>`, verifies the binary runs (`rtk --version`), and places it on `PATH`. Pin a specific version in a comment so the build is reproducible. Build MUST fail on install error — no silent fallback.
2. **Entrypoint**: extend the existing `case "${BACKEND}"` switch. After the CLI install branch, add a second guard:
   ```
   if [ "${RTK_ENABLED:-0}" = "1" ]; then
     case "${BACKEND}" in
       codex|claude) rtk init -g || echo "WARNING: rtk init failed" >&2 ;;
       *) echo "WARNING: RTK_ENABLED=1 but backend ${BACKEND} has no supported rtk hook — skipping" >&2 ;;
     esac
   fi
   ```
3. **Config wiring**: `PerformerEndpointConfig.env` already flows into `docker run -e KEY=VAL` (task 5 of 058 added this). Setting `env: {BACKEND: codex, RTK_ENABLED: "1"}` on `codex-ephemeral` is the entire coordinare-side change.
4. **Rollout**: ship default-off. Operators flip `RTK_ENABLED: "1"` on one endpoint and compare against an unmodified peer for one card cycle.

**Why default off**: rtk rewrites command output that lands in the model's context. If a card fails because the model was missing detail rtk filtered out, we want a single env toggle to revert — not a redeploy of new images. After a measurement window we can flip the default in `config.example.yaml`.

**Test approach**:
- Image build is verified by CI (build fails if `rtk --version` errors).
- Entrypoint behaviour is shell logic — a small `bats`-style or shellcheck-driven test, or just an integration smoke check that runs the entrypoint with `BACKEND=codex RTK_ENABLED=1` and greps for the expected log line.
- Coordinare-side env propagation: if not already covered by 058 task 5's tests, add one assert that `RTK_ENABLED` flows through `_build_job_payload` → `docker run -e`.

**Measurement plan** (not a test, an observation step before broadening rollout):
1. Pick 5 representative cards from recent history.
2. Run them twice each, once with `RTK_ENABLED=0` and once with `RTK_ENABLED=1`, same backend/model/seed where possible.
3. Compare input-token totals reported by the backend; tally pass/fail. SC-003 success criterion: ≥50% reduction on Bash output, no regression in pass-rate.

---

## Future fixes

Appended as live QA surfaces them. Keep each section self-contained: root cause, approach, files, test plan.
