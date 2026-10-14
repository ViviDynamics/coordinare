# 133 tasks

- [x] T001 Tests first: `tests/unit/test_133_run_viewer_api.py` (auth, paging, traversal and symlink refusal, unparsable lines, preview and full field, JSONL and Markdown streaming, missing run, run lookup, card listing, search)
- [x] T002 Tests first: `tests/js/runs_checks.js` and `tests/unit/test_133_runs_js.py` (pure rendering, filtering, anchors, escaping, window maths)
- [x] T003 Tests first: `tests/e2e/test_133_transcript_viewer.py` (deep link, expand, filter, jump to error, copy link, 2,000-event responsiveness, hostile transcript)
- [x] T004 `run_viewer.py`: path validation, safe open, line streaming, header, listing, summary, paging, search, full field, downloads
- [x] T005 `routers/runs.py`: JSON routes and page routes; register in `app.py` and `pages.py`; deferred lists in `_registry.py`
- [x] T006 `runs.js`: pure helpers, then the page controller (routing, window, lazy output, search, filters, copy link, persona diff)
- [x] T007 Wire-up: `assets.py`, `app.js` router and `showPage`, `dashboard.html` section, `shell.css`
- [x] T008 Links: card detail runs per stage (`performers.js`), run detail transcript link (`performers.js`), activity rows (`activity_streams.js`)
- [x] T009 Verify: ruff, mypy, ruff baseline, quality guard, unit and js tests, e2e, full tree

Notes
- T008: the card detail gets a runs list per stage (`hydrateCardRuns`, fed by `GET /api/card-runs/{card}`). The #147 timeline and #150 History are not on main, so they carry no link.
- T003: the browser checks the 2,000-event first paint and scroll steps against generous bounds, and bounds the DOM rows as well as the time.
