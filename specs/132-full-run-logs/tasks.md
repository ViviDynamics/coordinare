# Tasks: 132 full run logs

- [x] T001 Spec, plan and this task list (`specs/132-full-run-logs/`).
- [x] T002 Tests for the writer, collector, root guard and retention (`tests/unit/test_132_run_log.py`).
- [x] T003 Tests for the runtimes' `iter_logs` with docker and kubernetes fakes (`tests/unit/test_132_run_log_runtimes.py`).
- [x] T004 Tests for teardown ordering, bounds, failure, cancellation and tail redaction (`tests/unit/test_132_run_log_teardown.py`).
- [x] T005 Tests for the route: auth, traversal, paging, download, missing file (`tests/unit/test_132_run_log_route.py`).
- [x] T006 Implement `services/run_log.py`.
- [x] T007 Implement `iter_logs` in the runtime protocol, docker and kubernetes runtimes.
- [x] T008 Wire capture into the `http_performer_service` teardown; tail redaction; secrets remembered for all runs.
- [x] T009 `RunLogsConfig`, `__main__` wiring, daemon retention loop.
- [x] T010 Dashboard route, MCP deferred prefix, card link in `performers.js`.
- [x] T011 E2E test for the link (`tests/e2e/test_132_full_run_log.py`).
- [ ] T012 Verification: ruff, mypy, ruff baseline, quality guard, full pytest, e2e.
