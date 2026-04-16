# Tasks: 045 — Hotfix: Retry Budget & Backend Visibility

## Phase 1: Implementation (all done inline)

- [x] T001 Remove try/except TransportError in `src/coordinare/services/agent_service.py:check_status` — let errors propagate to monitor_performer
- [x] T002 Change transport.skipping_non_json_line from debug to warning in `src/coordinare/transport/subprocess_transport.py`
- [x] T003 Add move_card(IN_REVIEW) in session_expired handler in `src/coordinare/graph/nodes/monitor_performer.py`

## Phase 2: Tests

- [x] T004 Write test: agent_service.check_status re-raises TransportError (not swallows) in `tests/unit/services/test_agent_service.py`
- [x] T005 Write test: monitor_performer increments system_error_count when check_status raises TransportError
- [x] T006 Verify existing transport tests pass with warning-level log change

## Phase 3: Validation

- [x] T007 Run `.venv/bin/pytest tests/ -q` — all coordinare tests pass
- [x] T008 Run `.venv/bin/ruff check src/ tests/` — lint clean
