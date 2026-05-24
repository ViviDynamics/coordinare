# 071 Tasks — CI Log Inlining

## Phase 1 — Performer-side inlining (FR-001 — FR-003)

- [x] T001 Add `CI_LOG_INLINE_MIN_OUTPUT_CHARS` and `CI_LOG_INLINE_MAX_CHARS` to performer `Settings`.
- [x] T002 Extend `_format_check_failures` (or a sibling helper) in `agent/performer/src/performer/main.py` to fetch and inline log tails when `output.text` is short. Pure best-effort: any fetch error degrades to the current body.
- [x] T003 Unit test: failing check with empty `output.text` → relay body contains the fetched log tail.
- [x] T004 Unit test: `get_check_run_logs` raising → relay body falls back to existing format (no exception bubbles up).

## Phase 2 — Coordinare-side inlining (FR-004 — FR-005)

- [x] T005 Add `pr_checks_bounce_log_max_chars` to `NotificationsConfig` (default 6000, `ge=0`).
- [x] T006 Add `GitHubService.fetch_failed_job_log(owner, repo, job_id, max_chars)` that follows the 302 to S3 without auth header and tails to `max_chars`. Never raises.
- [x] T007 Add `_parse_job_id_from_details_url` helper in `monitor_performer.py`.
- [x] T008 Enrich BOUNCE body in `_handle_pr_checks_gate` to inline log tails per failed check. Fetch failures fall back to current name-only body.
- [x] T009 Unit test: monitor_performer BOUNCE inlines log tails when fetch returns content.
- [x] T010 Unit test: monitor_performer BOUNCE falls back to current body when fetch returns `""`.
- [x] T011 Unit test: empty-rollup / no-CI repos make zero log-fetch calls (FR-006).

## Phase 3 — Verification

- [x] T012 `.venv/bin/ruff check` on changed files clean.
- [x] T013 Full coordinare unit suite passes.
- [x] T014 Performer unit suite passes.
