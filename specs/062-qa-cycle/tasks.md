# Tasks: QA Cycle 062

Rolling QA iteration. New fixes are appended as live testing surfaces them; each fix is self-contained with its own task block.

## Fix 1 — Force-bootstrap preflight for performer registration

- [X] Locate `trigger_env_bootstrap` endpoint in `src/coordinare/dashboard.py` (insertion site after the `env_cache_service is None` 503 branch)
- [X] Insert preflight: look up `sym_cfg.env_bootstrap_performer_id` in `daemon.state["performer_services_by_id"]`
- [X] Return 503 with `{"error": "...", "performer_id": ...}` body when the performer service handle is missing
- [X] Preserve existing precedence: env_cache_service None → performer missing → cache state not initialised → bootstrap in flight → 202
- [X] Update `_attach_env_cache` helper in `tests/unit/test_dashboard.py` to seed `performer_services_by_id` so existing tests still pass under the stricter check
- [X] Update `test_force_env_bootstrap_no_cache_state_returns_503` to seed the same handle
- [X] Add `test_force_env_bootstrap_missing_performer_service_returns_503` — asserts 503, error names performer id, `body["performer_id"]` set
- [X] Run `.venv/bin/pytest tests/unit/test_dashboard.py -k env_bootstrap` → all green
- [X] Run `.venv/bin/ruff check src/coordinare/dashboard.py tests/unit/test_dashboard.py` → clean
- [X] Playwright test: clicking the Force-bootstrap button on a misconfigured symphony surfaces the daemon's 503 error (`performer_id`) in `#sym-list-msg` (`test_symphonies_page_bootstrap_503_shows_performer_id_error`)

## Spec scaffolding

- [X] Create `specs/062-qa-cycle/spec.md` (User Story 1, FR-001..FR-004, SC-001/SC-002, Out of Scope)
- [X] Create `specs/062-qa-cycle/plan.md` (Fix 1 detail + Future fixes placeholder)
- [X] Create `specs/062-qa-cycle/tasks.md` (this file)
- [X] Create branch `062-qa-cycle`

## Fix 2 — RTK token compression in performer containers

- [X] Pick a pinned `rtk` release version; record it in a comment in `agent/performer/Dockerfile.base` (v0.39.0)
- [X] Add `rtk` install step to `agent/performer/Dockerfile.base` (download, sha256-verify, install, `rtk --version`); build MUST fail on error
- [X] Extend `agent/performer/entrypoint.sh`: when `RTK_ENABLED=1`, run `rtk init -g` for `BACKEND in {codex, claude}`; log warning + skip for other backends
- [X] Keep `RTK_ENABLED` default off; document the toggle in `config.example.yaml` on `codex-ephemeral`
- [X] Verify `PerformerEndpointConfig.env` propagation already covers `RTK_ENABLED`; added regression test `test_start_ephemeral_propagates_config_env`
- [X] Rebuild performer images (`base` + `full`) with the rtk install step — `bin/build --docker` green locally; `bin/build` now asserts `rtk --version` runs in the base image
- [X] Smoke test: container starts with `BACKEND=codex RTK_ENABLED=1` and entrypoint logs the `rtk init` line — covered by `tests/unit/test_performer_entrypoint.py::test_entrypoint_runs_rtk_init_for_codex` (+ claude variant)
- [X] Smoke test: container starts with `BACKEND=codex` (no `RTK_ENABLED`) and entrypoint skips rtk init — covered by `test_entrypoint_skips_rtk_when_disabled`; non-fatal-failure + unsupported-backend + no-backend branches also covered
- [X] `bin/build --docker` step: `rtk` compression wrapper produces output (smoke that the binary actually runs end-to-end, not just `--version`)
- [X] `bin/build --docker` step: `rtk init -g` inside the container writes RTK.md, CLAUDE.md `@RTK.md` reference, and `filters.toml` — proves the hook wiring, not just the call
- [ ] Measurement run: 5 representative cards × {RTK on, RTK off}; record input-token deltas and pass/fail; attach results to PR
- [ ] Decide based on measurement: flip default on, leave default off, or revert

## Fix 3 — PR #62 (spec 046) review followups

- [X] `services/github.py`: classify `check_issue_state` return values (definitive vs transient) in docstring
- [X] `services/github.py`: case-insensitive `/graphql` strip and `/api` path detection in `_rest_api_base`
- [X] `services/dependency.py`: demote transient (`auth_error`/`api_error`/`transient`) UNRESOLVABLE deps to PENDING instead of permanently marking unresolvable
- [X] `services/dependency.py`: docstring on `detect_cycles` clarifying that disjoint cycles are reported separately (no BFS-merge bug)
- [X] `graph/nodes/assess_card.py`: explicit `isinstance(int|str)` + bool reject when coercing assessor `dependencies` list
- [X] `graph/nodes/check_board.py`: URL allowlist (`_allowed_github_host` + `_safe_issue_url`) so dashboard innerHTML cannot render attacker-controlled hosts
- [X] `graph/nodes/check_board.py`: idempotent UNRESOLVABLE move/comment via `state["_dep_announcements"]` signature
- [X] `graph/nodes/check_board.py`: idempotent cycle move/comment using the same announcement signature pattern
- [X] `.venv/bin/ruff check` clean on all four files
- [X] `.venv/bin/pytest tests/unit -q` — 2244 passed, 2 skipped

## Fix 4 — Dashboard board swimlane (TODO / BLOCKED / IN_PROGRESS / IN_REVIEW)

- [X] `services/github.py`: extend `POLL_BOARD_QUERY` Issue with `timelineItems(last:20, itemTypes:[CROSS_REFERENCED_EVENT])` exposing PR `url`/`state`/`merged`
- [X] `services/github.py`: build `pr_urls` map per item; priority OPEN > MERGED > CLOSED so live PRs win over stale closed references; include in `poll_board` return
- [X] `graph/nodes/check_board.py`: stash `state["_board_pr_urls"]` alongside `_board_titles` / `_board_issue_numbers` / `_board_issue_urls`
- [X] `graph/state.py`: add `board_titles` / `board_issue_numbers` / `board_issue_urls` / `board_pr_urls` to `SymphonyRuntimeState`
- [X] `daemon.py`: post-cycle sync copies the four `_board_*` maps from `self._state` onto `sym_state`
- [X] `dashboard.py:_build_symphonies_data`: expose the four maps + `active_sessions` in the symphony state payload
- [X] `dashboard.py` HTML: replace `#active-work-card` / `#awaiting-review-card` with a single `#swimlane-section` 4-column grid (TODO / BLOCKED / IN_PROGRESS / IN_REVIEW)
- [X] `dashboard.py` JS: rewrite `renderActiveWorkPanels` as swimlane renderer — cards link to issue URL, PR badge when present, live phase/stage/elapsed when a session is active; BACKLOG and DONE never rendered
- [X] `dashboard.py` CSS: add `.swimlane-grid` / `.swimlane-col` / `.swimlane-card` styles
- [X] Tests: `test_poll_board_pr_urls_prefers_open_over_merged_and_closed`, `_falls_back_to_merged`, `_empty_when_no_cross_references`
- [X] `.venv/bin/ruff check` clean; `.venv/bin/pytest tests/unit -q` — 2247 passed, 2 skipped
- [ ] Live smoke: launch coordinare with `.env` + `config.yaml` and visually confirm the swimlane renders cards in all four columns with working issue / PR links

## Future fixes

Appended as live QA surfaces them. Each new fix gets its own `## Fix N — <name>` task block.
