# Tasks: QA Cycle 065

Rolling QA iteration. New fixes are appended as live testing surfaces them.

## Spec scaffolding

- [X] Create `specs/065-qa-cycle/spec.md`
- [X] Create branch `065-qa-cycle`
- [X] Create `specs/065-qa-cycle/plan.md`
- [X] Create `specs/065-qa-cycle/tasks.md` (this file)

## Fix 1 — LangGraph state-merge schema for env_cache_service / performer_services_by_id

- [X] Add `env_cache_service: Any` and `performer_services_by_id: dict[str, Any]` to `CoordinareState` TypedDict in `src/coordinare/graph/state.py`
- [X] Seed both keys in `initial_state()` (`None` and `{}` respectively) with an inline comment explaining the LangGraph state-merge requirement
- [X] Add `test_service_handles_declared_in_state_schema` to `tests/unit/test_state.py` (annotations check)
- [X] Add `test_initial_state_seeds_service_handles` (seed presence)
- [X] Add `test_service_handles_survive_langgraph_invocation` (real `StateGraph(CoordinareState)` round-trip with sentinel values)
- [X] Live verify: restart daemon, POST `/api/symphonies/website/env-bootstrap`, observe HTTP 202 and second performer container coming alive
- [X] `.venv/bin/pytest tests/unit/test_state.py` → all green

## Fix 2 — IN_PROGRESS fall-through for multi-card pickup (US2, FR-008a)

- [X] Diagnose: `src/coordinare/graph/nodes/check_board.py:380-449` returns early in every IN_PROGRESS path, never reaching the 035 multi-card TODO pickup at line ~794
- [X] Restructure the IN_PROGRESS branch with `if max_cards > 1` / `else` split mirroring the 061 IN_REVIEW pattern
- [X] Multi-card path: re-adopt every uncovered IN_PROGRESS card into `active_sessions` as `phase="monitoring_agent"` (slot-consuming)
- [X] Multi-card path: per-session invocation whose `current_card` is in IN_PROGRESS short-circuits with `phase="monitoring_agent"`; otherwise fall through to TODO pickup
- [X] Single-card path: preserve existing orphan re-adopt + dispatch / monitoring_agent default verbatim
- [X] Add `test_check_board_multicard_readopts_in_progress_and_picks_up_todos` — 1 IN_PROGRESS + 2 TODO + cap=3 → 3 active_sessions
- [X] Add `test_check_board_multicard_per_session_in_progress_preserves_monitoring_agent`
- [X] Add `test_check_board_singlecard_in_progress_still_short_circuits`
- [X] `.venv/bin/pytest tests/unit/graph/nodes/test_check_board.py` → all green (78 passed)
- [X] `.venv/bin/ruff check src/coordinare/graph/nodes/check_board.py tests/unit/graph/nodes/test_check_board.py` → clean
- [X] Full `.venv/bin/pytest tests/unit -q` → 2539 passed, 2 skipped
- [X] Live verify: restart daemon with the fix, observe ≥2 cards transition from TODO → IN_PROGRESS within a couple of cycles (active_sessions=3 within 2 min of 01:47 UTC restart, cards 74/101/111 concurrent)
- [X] Commit on `065-qa-cycle` (41029d1)

## Fix 3 — Advisory comment dedup (security performer)

- [X] Add `list_pr_comments` to `agent/performer/src/performer/github.py`
- [X] Add `_advisory_fingerprint` + `_parse_advisory_header` helpers in `agent/performer/src/performer/main.py`
- [X] Rewrite the security advisory post loop to fetch existing comments, build fingerprint set, skip duplicates (cross-cycle + intra-batch), log skip events, fall through on list failure
- [X] Add `test_advisory_comments_dedup_against_existing_pr_comments`
- [X] Add `test_advisory_comments_posted_when_no_matching_existing`
- [X] Add `test_advisory_comments_intra_batch_dedup`
- [X] `.venv/bin/pytest agent/performer/tests/unit -q` → 610 passed
- [X] `.venv/bin/ruff check agent/performer/src/performer/main.py agent/performer/src/performer/github.py agent/performer/tests/unit/test_main.py` → clean
- [X] Live verify on PR #133: restart coordinare; observe no new duplicate advisory comment after a security cycle (5 security cycles since 01:47 UTC restart, zero new advisory comments — last advisory was 23:53 UTC, pre-restart)
- [X] Commit on `065-qa-cycle` (800742a)

## Fix 4 — Closer feedback loop (empty-comments changes_requested)

### 4a — Closer prompt: do not gate on remote CI

- [X] Update the `closer` PR-checks directive in `src/coordinare/services/persona_service.py` (`_CLOSER_PR_CHECKS_DIRECTIVE`, lines 41-49) to explicitly NOT gate on remote CI / status checks (that is 064's job); closer evaluates code, thread resolution, PR hygiene
- [X] Note the layering rule inline in the prompt (closer approves code → coordinare's 064 gate holds until checks pass → handoff to humans)

### 4b — Forward `review_body` in `changes_requested` PerformerResponse

- [X] Add `body: str | None` field to `PerformerResponse` (`agent/performer/src/performer/protocol.py`)
- [X] In `agent/performer/src/performer/main.py` reviewer/closer block, populate `body=review_body or None` on the `changes_requested` response
- [X] Coordinare side: in `monitor_performer.py:1248-1267`, when structured `comments` is empty but `body` is present, synthesize `[{"body": body, "author_login": "coordinare"}]` as `relay_feedback`
- [X] Add `test_reviewer_changes_requested_forwards_body` in `agent/performer/tests/unit/test_main.py`

### 4c — Coordinare safety net: block on no-actionable-feedback bounce

- [X] In `src/coordinare/graph/nodes/monitor_performer.py` `changes_requested` branch, detect `len(comments) == 0 AND not body` → set `phase="blocked"`, populate `system_error_reason`, populate `open_questions`, return without re-dispatching to implementer
- [X] Add `test_changes_requested_with_no_actionable_feedback_blocks`
- [X] Add `test_changes_requested_with_body_only_relays_body_as_comment`

### 4d — Distinguish Bot Closer Review from Bot Review in PR comments

- [X] In `agent/performer/src/performer/main.py:1222`, set header to `"Bot Closer Review"` when `perf.role == "closing_review"` else `"Bot Review"`
- [X] Add `test_closing_review_uses_distinct_pr_review_header`

### Live verify + commit

- [X] `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer.py` → 103 passed
- [X] `.venv/bin/pytest agent/performer/tests/unit/test_main.py -k "reviewer or closing or changes_requested"` → 13 passed
- [X] Full `.venv/bin/pytest tests/unit -q` → 2541 passed, 2 skipped (no regressions)
- [X] Full agent/performer `pytest tests/unit -q` → 612 passed
- [X] `.venv/bin/ruff check` on all changed files → clean
- [ ] Restart coordinare against PR #133; observe: closer no longer rejects on pending CI, OR if it does, the rejection text is relayed (4b) or the card blocks (4c) instead of looping to implementer
- [ ] Commit on `065-qa-cycle`

## Fix 5 — Multi-card pickup dead in steady state

### 5a — Slot-aware phase-preservation guard

- [X] In `src/coordinare/graph/nodes/check_board.py:387–410`, change the `dispatching|monitoring_performer|blocked` + `current_card is not None` early-return so it returns only when `max_cards <= 1` OR all concurrency slots are already filled (count non-`NON_SLOT_PHASES` sessions). Open slots in multi-card mode fall through to in_progress re-adopt + TODO pickup.

### 5b — Preserve in-flight phase across in_progress re-adopt

- [X] In `check_board.py:474–482` (the per-session-owns-in_progress-card path), don't overwrite `state["phase"]` with `"monitoring_agent"` when the incoming phase is already `monitoring_performer | dispatching | blocked`.

### 5c — One primary per cycle picks up TODOs

- [X] In the same block, gate the fall-through to TODO pickup behind `current_id == min(in_progress)` AND open-slot count `< max_cards`. Non-primary per-session invocations return after re-adopt so the GitHub-side-effect paths in TODO pickup (move_card / dep-block comments) fire at most once per cycle.

### Live verify + commit

- [X] Add `test_check_board_multicard_per_session_monitoring_performer_falls_through_to_todo_pickup`
- [X] `.venv/bin/pytest tests/unit/graph/nodes/test_check_board.py -q` → 79 passed
- [X] Full `.venv/bin/pytest tests/unit -q` → 2542 passed, 2 skipped
- [X] `.venv/bin/ruff check` on changed files → clean
- [ ] Restart coordinare with `max_concurrent_cards=3` against a board with one IN_PROGRESS card and two+ TODOs; observe daemon log line `new_session_registered` for at least two more cards on the next cycle, and `active_sessions=3` thereafter
- [X] Commit on `065-qa-cycle` (05313c6)

## Fix 6 — Closer PR-checks gate must not FORWARD on visible pending when branch protection is unreadable

Observed on PR #134 / card #111: closer marked the card "ready for human review" at 14:40 UTC while the PR's combined build status was still `pending`. With the GitHub App auth used in production, `branchProtectionRules` returns null in GraphQL so `branch_protection_readable=False`; with the default `treat_unknown_required_as="pass"`, the policy zeroed the required set and FORWARDed blindly.

- [X] Diagnose: `src/coordinare/services/pr_checks_policy.py:75-86` — BP-unreadable + mode=pass cleared `required = []` and fell through to FORWARD even with visible pending checks
- [X] Tighten policy: when BP unreadable + mode=pass, surface visible pending checks → HOLD (subject to existing `pending_timeout`); preserve advisory-failure behavior so `test_unreadable_bp_pass_mode_ignores_non_required_failure` still holds
- [X] Add `test_unreadable_bp_pass_mode_holds_on_visible_pending`
- [X] Add `test_unreadable_bp_pass_mode_pending_timeout_bounces`
- [X] `.venv/bin/pytest tests/unit/services/test_pr_checks_policy.py -q` → 19 passed
- [X] Full `.venv/bin/pytest tests/unit -q` → 2544 passed, 2 skipped
- [X] `.venv/bin/ruff check` on changed files → clean
- [ ] Live verify on PR #134: restart coordinare with the fix, observe `closer.pr_checks.decision action=HOLD` while CI still pending, then FORWARD only after build green
- [X] Commit on `065-qa-cycle` (4898159)

## Fix 7 — Multi-card stage survival across restart (closing_review demotion)

Observed on PR #134 / card #111: after a daemon restart, card #111 was re-adopted but `performer_stage` reverted to `implementing`, demoting a closer-stage card back to the implementer. Root cause: `check_board.py` re-adopt path calls `create_session_from_card` which hardcodes `performer_stage="implementing"`, and `WorkflowSnapshot` only persisted single-card top-level stage — multi-card per-session stage was lost.

### 7a — check_board re-adopt honours snapshot-restored stage (narrow)

- [X] Add `_snapshot_stage_for_card(state, card_id)` helper to `src/coordinare/graph/nodes/check_board.py` (prefers `active_sessions[card_id].performer_stage`, falls back to top-level `performer_stage` when `current_card.id` matches — covers v1 snapshots)
- [X] IN_PROGRESS re-adopt (~line 484): after `create_session_from_card`, override `sess["performer_stage"]` with the snapshot stage when present
- [X] IN_REVIEW re-adopt (~line 372): same override
- [X] Add `test_check_board_in_progress_readopt_preserves_snapshot_stage_v1`
- [X] Add `test_check_board_in_progress_readopt_uses_persisted_session_stage`

### 7b — Persist `active_sessions` in `WorkflowSnapshot` (broad, v1→v2 schema)

- [X] Bump `CURRENT_SCHEMA_VERSION` 1 → 2; introduce `MIN_SUPPORTED_SCHEMA_VERSION = 1`; widen `load()` to accept the range so v1 snapshots forward-compat with empty `active_sessions`
- [X] Add `PersistedSession(BaseModel)` to `state_store.py` (card_id, performer_stage, phase, lifecycle_completed_at, processed_review_ids, open_questions, card_clarifications, relay_feedback, system_error_*, requirements_changed)
- [X] Add `active_sessions: dict[str, PersistedSession]` to `WorkflowSnapshot`
- [X] Daemon: `_persist_active_sessions(active_sessions)` helper converts live session dicts into `PersistedSession`, normalising sets→sorted lists and datetimes
- [X] Wire `_persist_active_sessions` into `_build_snapshot`
- [X] Daemon `_restore_from_snapshot`: rehydrate `state["active_sessions"]` from `snapshot.active_sessions`, seeding `current_card` for the matching `active_card_id` and a stub `{"id": card_id}` for the rest
- [X] Add `test_save_load_round_trip_active_sessions`
- [X] Add `test_load_v1_snapshot_forward_compat`

### Live verify + commit

- [X] `.venv/bin/pytest tests/unit/test_state_store.py tests/unit/graph/nodes/test_check_board.py -q` → 101 passed
- [X] Full `.venv/bin/pytest tests/unit -q` → 2548 passed, 2 skipped
- [X] `.venv/bin/ruff check` on changed files → clean
- [ ] Restart coordinare against PR #134; observe card #111 keeps `performer_stage=closing_review` across the restart (unblocks Fix 6 live verify)
- [ ] Commit on `065-qa-cycle`

## Fix 8 — Re-gate PR checks while card sits in monitoring_pr (don't bother humans on red CI)

Observation while reviewing the closer flow: once the closer forwards a card to IN_REVIEW, a fresh push that turns CI red is not re-gated — the human is paged for review even though the PR is no longer mergeable. The PR-checks gate currently only runs at the moment the closer hands off; nothing watches subsequent HEAD commits. Fix: run the same gate from `monitor_pr` on every tick.

- [X] In `src/coordinare/graph/nodes/monitor_pr.py`, after pr_node_id is resolved, call `_evaluate_pr_checks_gate(state, card_id, pr_url)` from `monitor_performer` and apply its updates
- [X] On BOUNCE (gate sets `phase=dispatching` + `performer_stage=implementing` + `relay_feedback`), additionally move the card on the GitHub board IN_REVIEW → IN_PROGRESS and update `current_card.status`
- [X] On HOLD (gate's native `phase=monitoring_performer`), override phase back to `monitoring_pr` so the PR-phase polling loop keeps ticking
- [X] On FORWARD / disabled: apply any cache updates from the gate and continue with the existing review-polling path
- [X] Add `test_monitor_pr_fix8_bounces_to_implementer_on_failed_checks`
- [X] Add `test_monitor_pr_fix8_holds_when_required_checks_pending`
- [X] Add `test_monitor_pr_fix8_forwards_to_reviews_when_checks_pass`
- [X] `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_pr.py tests/unit/graph/nodes/test_monitor_performer.py -q` → 128 passed
- [X] `.venv/bin/ruff check` on changed files → clean
- [ ] Live verify: push a red commit to an IN_REVIEW PR; observe `monitor_pr.checks_gate_bounce` log line, the card returning to IN_PROGRESS on the board, and the implementer re-dispatched with the failing check name in its prompt
- [ ] Commit on `065-qa-cycle`

## Fix 9 — Performer integration tests leak stopped containers

Observation: running the performer integration suite (`agent/performer/tests/integration/test_dockerfile_{base,slim,full}.py`) leaves "Exited" containers behind because the cleanup `finally` blocks call `docker stop` + `docker rmi` but never `docker rm`. Stale containers accumulate over time in the user's local Docker.

- [X] `test_dockerfile_base.py`: `docker run -d` → `docker run -d --rm`; finally `docker stop` → `docker rm -f` (safety net if `--rm` doesn't fire)
- [X] `test_dockerfile_slim.py`: same change in both functions (basic + browser toggle)
- [X] `test_dockerfile_full.py`: same change in the per-backend loop
- [X] `.venv/bin/ruff check` on changed files → clean
- [ ] Run `bin/build --all` (includes docker integration) to confirm no leaked containers and tests still pass
- [ ] Commit on `065-qa-cycle`

## Fix 11 — Performer can't infer services without coordinare on PYTHONPATH (`coordinare_not_available`)

Observation: live performer runs against the containerized base image logged `coordinare_not_available` and skipped service inference, because `src/coordinare/services/service_inference/` was not reachable from inside the performer wheel/image. The performer carried a defensive `try/except ImportError → coordinare_not_available` fallback rather than depending on the code directly.

- [X] Extract `src/coordinare/services/service_inference/` → standalone package `packages/service_inference/` (`coordinare_service_inference`)
- [X] New `packages/service_inference/pyproject.toml` (`coordinare-service-inference`, anthropic/jinja2/pydantic/stamina/structlog)
- [X] Coordinare `pyproject.toml` + performer `pyproject.toml` depend on `coordinare-service-inference` via `[tool.uv.sources]` editable path
- [X] Rewrite all consumer imports (`coordinare.services.service_inference` → `coordinare_service_inference`) in `src/coordinare/services/env_cache.py`, `agent/performer/src/performer/main.py`, and all `tests/unit/services/test_service_inference_*` + integration tests
- [X] Remove the `coordinare_not_available` ImportError fallback in `performer/main.py` — the package is now a real installed dep, missing it should fail loudly
- [X] `Dockerfile.base`: switch build context to repo root, COPY `packages/service_inference/`, install before performer, strip in-image `[tool.uv.sources]` path block via `sed` (relative path doesn't exist inside the image)
- [X] Update `bin/build`, `.github/workflows/pr-ci.yml`, `.github/workflows/main-branch-build.yml`, `tests/integration/test_containerized_performer.py` to use repo-root build context for the base image
- [X] `.venv/bin/ruff check src tests agent/performer/src packages/service_inference/src` → clean
- [X] `.venv/bin/pytest tests/unit/services/test_service_inference_*.py tests/unit/services/test_env_cache_phase4.py tests/contract/test_env_cache_payload.py -q` → 147 passed
- [ ] Live verify: launch performer against base image and observe service inference runs (no `coordinare_not_available` log line)
- [ ] Commit on `065-qa-cycle`

## Fix 12 — `card_stuck` notifications unrouted

Observation: `daemon.py` raises a `card_stuck` notification when a card has been in the same phase too long (028), but neither `config.yaml` nor `config.example.yaml` had a routing entry, so it was logged as `notification_unrouted` and silently dropped.

- [X] Add `event_type: card_stuck → [slack-ops]` routing to `config.yaml`
- [X] Mirror the entry in `config.example.yaml` and document `card_stuck` in the event-type comment block
- [ ] Live verify: trigger a card_stuck condition and confirm the slack-ops channel receives the notification (no `notification_unrouted` log line)
- [ ] Commit on `065-qa-cycle`

## Fix 13 — Architect path must accept workspace-written plan.md

Observation: card #101 architect blocked with `"Backend produced an empty architecture plan"`. The codex backend ran cleanly (37s) and wrote the plan via `apply_patch` into the workspace, but `BackendStatus.output` is only fed by assistant text (see `backends/codex.py:490-518`), so `main.py:1055-1064` saw empty output and bailed before checking disk. Bigger contexts don't fix this — the plan never travels through `output`.

- [X] In `agent/performer/src/performer/main.py` architecting branch (≈lines 1055-1092): when `backend_status.output` is empty, probe `{_doc_folder(perf.score)}/plan.md` under `perf.stand.path`; if present and non-empty, read it (and optional `{folder}/tasks.md`) as the plan/tasks content and skip the `---TASKS---` split
- [X] Keep the existing `commit_file` calls — they're already idempotent (no staged diff → no-op commit) and will push if the model wrote but did not push
- [X] Only return the `"Backend produced an empty architecture plan"` error when inline output is empty AND no workspace `plan.md` exists
- [X] Log `source="workspace"` vs `source="inline"` on the success path for future triage
- [X] Unit test: backend returns `output=None` + workspace pre-populated with `docs/cards/<N>-<slug>/plan.md` → response is `status="plan_committed"`, `perf.plan_path` set, no error
- [X] Unit test: backend returns `output=None` + no workspace plan.md → still `status="error"` with the same `error_reason` (regression guard)
- [X] Existing inline-output architect test continues to pass (140 tests in test_main.py green)
- [X] `.venv/bin/ruff check agent/performer/src/performer/main.py` → clean
- [ ] Live verify: re-run card #101 against codex; architect advances to `plan_committed` without manual intervention
- [ ] Commit on `065-qa-cycle`

## Fix 14 — CI fix loop: no-progress detection + lower max-attempt cap

Observation: PRs #135/#136 on ViviDynamics/website burned all 25 CI fix attempts on identical failures every cycle. Lower the cap and bail early when the failure signature repeats.

- [X] `agent/performer/src/performer/config.py`: lower `CHECK_MAX_ATTEMPTS` default 25 → 8; add `CHECK_NO_PROGRESS_LIMIT: int = 2`
- [X] `agent/performer/src/performer/models.py`: add `last_check_failure_signature` + `check_no_progress_streak` to `Performance`
- [X] `agent/performer/src/performer/main.py`: new `_failure_signature` helper; `_poll_check_runs` fail branch blocks with "no progress across N attempts" when streak ≥ limit, before the max-attempts check
- [X] Unit tests: signature-stable blocks at limit; differing signature resets streak
- [X] `.venv/bin/ruff check` on changed files → clean
- [X] `.venv/bin/pytest agent/performer/tests/unit/test_main.py` → passing
- [ ] Live verify: trigger a CI failure the model can't fix in 2 attempts; card blocks with `no progress` message instead of grinding to 8
- [X] Commit on `065-qa-cycle` (combined with Fix 15)

## Fix 15 — Tool-driven CI failure inspection (`performer-fetch-ci-log` shim)

Observation: Even with Fix 14, the relay only carried the Check Run's structured `output` — not the workflow log. Replace passive log-inlining with a CLI shim the model can call on-demand, same pattern as `performer-upload-screenshot`.

- [X] `agent/performer/src/performer/github.py`: add `get_pr_head_sha(owner, repo, pr_number, token)`
- [X] `agent/performer/src/performer/cli.py`: new `fetch_ci_log_cli` with `--list` and `--check NAME [--lines N]`; reads `PERFORMER_GH_TOKEN/OWNER/REPO/ISSUE` from env
- [X] `agent/performer/pyproject.toml`: register `performer-fetch-ci-log = "performer.cli:fetch_ci_log_main"`
- [X] `agent/performer/src/performer/main.py`: replace inline log-fetching with a short tool-hint listing failing checks as ready-to-paste `performer-fetch-ci-log --check '<name>'` commands plus `--list`
- [X] Drop unused `_format_check_failures_with_logs` and its `get_check_run_logs` import from main.py
- [X] Unit tests: `fetch_ci_log_cli` (missing env, empty list, list prints, check not found, check fetches log, API error); `get_pr_head_sha` (success / missing sha / empty token); relay includes tool-hint
- [X] `.venv/bin/ruff check` on changed files → clean
- [X] `.venv/bin/pytest agent/performer/tests/unit/{test_cli,test_main,test_github}.py` → 199 passed
- [ ] Live verify: trigger a CI failure on a real PR; the relay points at `performer-fetch-ci-log --check '…'`, the backend calls it, and the log tail informs the next turn
- [X] Commit on `065-qa-cycle`

## US4 — Un-blocking restarts feedback budget (FR-012..FR-016)

Already landed across earlier branches; recording here for completeness so the bookkeeping
matches the code. Single-card un-block reset path verified; multi-card gap fixed under Fix 16.

- [X] State schema: `feedback_cycle_count`, `total_feedback_cycles`, `triage_blocks` on `CoordinareState` (`src/coordinare/graph/state.py:157-165`)
- [X] Exhaustion path bumps monotonic counters in `monitor_performer._feedback_cycle_exhausted` (`monitor_performer.py:297-304`)
- [X] Dashboard surfaces all three (`src/coordinare/dashboard.py:491-493, 527-529`)
- [X] Single-card un-block detection resets `feedback_cycle_count` and emits `dispatcher.feedback_cycle_reset` (`check_board.py:1042-1051`)

## Fix 16 — Multi-card un-block feedback reset gap

- [ ] In `src/coordinare/graph/nodes/check_board.py` multi-card pickup (~lines 955-986), detect previously-blocked cards re-entering TODO and reset their per-session `feedback_cycle_count` to 0 before creating/refreshing the active session. Preserve `total_feedback_cycles` and `triage_blocks`.
- [ ] Detection signal: card id is present in `state["active_sessions"]` with `current_card.status == "BLOCKED"`, OR card id matches the top-level `current_card` with `status == "BLOCKED"`. Either condition + the card now appearing in `eligible_todo` means un-block.
- [ ] Emit `dispatcher.feedback_cycle_reset` log record with `{card_id, prior_count, total_feedback_cycles, triage_blocks, mode: "multi"}` so the reset is auditable.
- [ ] Unit test: `test_check_board_multicard_unblock_resets_feedback_cycle_count` — seed multi-card state with one card in `active_sessions` showing `status=BLOCKED` + `feedback_cycle_count=4`, place the same card in `eligible_todo`, run check_board, assert `feedback_cycle_count == 0` on the session, `total_feedback_cycles` and `triage_blocks` retained, log record emitted.
- [ ] Unit test: `test_check_board_multicard_fresh_pickup_no_reset_log` — different card transitioning TODO → IN_PROGRESS does not emit the reset log.
- [ ] `.venv/bin/pytest tests/unit/graph/nodes/test_check_board.py` → green
- [ ] `.venv/bin/ruff check` on changed files → clean
- [ ] Live verify: with `max_concurrent_cards=3`, un-block a card and observe `dispatcher.feedback_cycle_reset mode=multi` log line on the next cycle
- [ ] Commit on `065-qa-cycle`

## Fix 18 — `system_error_count` clobbered by dispatch success, retry budget never escalates

Symptom (live): card stuck in `reviewing → terminal_state=failed → handle_system_error.waiting attempt=1 → retry` loop indefinitely. `handle_system_error._MAX_RETRIES=3` is never reached because `attempt` resets to 1 every cycle.

Root cause: `src/coordinare/graph/nodes/dispatch_performer.py:720` unconditionally zeroed `system_error_count` on every successful dispatch. When `handle_system_error` re-dispatched the same card+stage after a post-dispatch model failure (e.g. backend `BACKEND_FORMAT_ERROR: review output was empty`), the dispatch succeeded → counter cleared → `monitor_performer` incremented it back to 1 → loop.

- [x] Guard the success-path reset in `dispatch_performer.py` so it only fires on a fresh dispatch (no `system_error_last_at` carried over, or `system_error_notified=True` indicating prior card's stale state). Mid-retry preserves the count so the budget can drain.
- [x] Update `test_success_resets_system_error_fields` doc — fresh-pickup / stale-from-prior-card case still resets.
- [x] New unit test `test_success_preserves_system_error_count_mid_retry` — sets `system_error_last_at`, `system_error_count=2`, dispatches successfully, asserts count is preserved at 2 (not reset).
- [x] `.venv/bin/pytest tests/unit/graph/` → 511 passed, no regressions
- [ ] Live verify: trigger an empty-output review failure (e.g. by running a card whose reviewer model returns ''), observe attempt counter increment 1 → 2 → 3 in `handle_system_error.waiting`, then `max_retries_exceeded` followed by `notification_service.dispatch` + `move_card BLOCKED`.
- [ ] Commit on `065-qa-cycle`

NOTE: Fix 18 only stops the *infinite loop*. The underlying "model returns empty output for the reviewer JSON contract" is a separate investigation — likely either prompt shape, model-provider config, or wire format. Tracked as **Fix 19 candidate** below.

## Future fixes (append as live testing surfaces them)

- [ ] US3 — kicked-back cards don't strand performers (FR-009..FR-011)
- [ ] US5 — performer CI ownership (FR-017..FR-022, absorbs spec 043)
- [ ] Fix 17 candidate — operator un-block comment threaded into next implementer dispatch as `relay_feedback` (root cause of the loop seen on website#70; needs its own spec scoping)
- [ ] Fix 19 candidate — reviewer (and possibly other JSON-contract roles) returning empty completions against `spark/qwen3.6:35b`. Performer logs: `backend.invalid_output.retrying ... failure_reason='was empty' output_preview='' stage=review`. Investigate: (a) prompt length / context overflow against the LiteLLM/spark endpoint, (b) stop-token or sampling param mis-config in codex's `~/.codex/config.toml` for that provider, (c) wire API mismatch (`responses` vs `chat`), (d) JSON-mode constraint rejected by the model. Possibly: bump `BACKEND_PARSE_RETRIES` default from 1 to 2 as a safety net.
  - [x] Diagnostic landed in `agent/performer/src/performer/backends/codex.py` `_handle_notification` → when `turn/completed` arrives with `status=completed` but every extraction path (assistant message text parts, agentMessage delta accumulator, turn summary) is empty, log `codex.turn_completed.empty_output` with item_types, item_roles, accumulator_len, usage, and a truncated raw turn dump. This is a one-shot diagnostic — next live empty-review will surface the actual codex payload shape so we know whether it's an empty assistant message, reasoning-only items, or no message at all.
  - [ ] Live capture: trigger a reviewer empty-output run; grab the `codex.turn_completed.empty_output` line from performer container logs; decide root cause from item shape.
