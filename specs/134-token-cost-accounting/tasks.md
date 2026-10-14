# Tasks: #134 token and cost accounting

- [x] T001 Spec, plan, tasks.
- [x] T002 Tests first: ledger, price table, cost fixture, capture, JS, e2e.
- [x] T003 `services/token_usage.py`: counts, ledger, tiers, rollups, price table, snapshot view.
- [x] T004 `run_transcript.py`: stage on usage records, `drain_usage`, `parts`, `TranscriptCollector.usage`.
- [x] T005 `http_performer_service.py`: stage in the target, observe after each accepted batch.
- [x] T006 `__main__.py`: rebuild the ledger from disk at boot.
- [x] T007 `config.py`: price table, validation, deprecated flat-rate fallback and warning; `config.example.yaml`.
- [x] T008 `graph/nodes/monitor/body.py`: stage in the target; card cost from the ledger.
- [x] T009 `dashboard/store.py`: `token_usage` block, per-session `usage`; drop `cost_per_million_tokens`.
- [x] T010 `dashboard_static/usage.js` (new), `performers.js`, `app.js`, `dashboard.html`, `assets.py` (script order).
- [x] T011 Update the superseded tests (138 snapshot keys, 119 cost rate, 122 and 429 JS slices, cost estimate checks).
- [x] T012 Docs: the configuration reference for `cost_tracking`.
