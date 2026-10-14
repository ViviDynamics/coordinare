# Tasks: Redact performer text in every dashboard field

- [x] T001 Inventory the dashboard viewer fields (store.py builders and every router), with entry point and redacted-before status. Recorded in `spec.md`.
- [x] T002 Write the failing tests first: `tests/unit/test_203_dashboard_redaction.py` (helper, entry points, dynamic snapshot and MCP walk, static writer guard).
- [x] T003 Add `redact_state_value` to `lib/redaction.py` (linear, walks lists and dicts).
- [x] T004 Redact the judge reason and evidence in `services/observer.py`.
- [x] T005 Redact stderr lines as buffered in `transport/subprocess_transport.py`, and the session title at fetch.
- [x] T006 Wrap every writer of a text field in `graph/nodes/**`, `services/**`, `daemon.py` and `cancel.py` through `redact_state_value`, keeping the raw `relay_feedback` copy for the implementer.
- [x] T007 Lint (`ruff check`, ratchet `scripts/ruff_baseline.py`), mypy (`-p coordinare`).
- [x] T008 Run the new module and the related dashboard and daemon modules.
- [ ] T009 Run the whole `tests` tree and the browser suite; report any machine-load failures by rerunning them alone.
- [ ] T010 Run the quality guard, commit locally (no push, no PR).
