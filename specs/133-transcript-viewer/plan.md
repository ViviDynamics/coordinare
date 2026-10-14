# 133 plan

## Decisions

- **D1 Reader module.** `src/coordinare/services/run_viewer.py`. It finds files through `transcript_path` and `sanitize_segment` from `run_transcript.py`. It has no writer state and touches the disk only to read. `run_viewer` imports `run_transcript`, never the reverse.
- **D2 Traversal.** A URL segment must already be in sanitized form (`sanitize_segment(x) == x`), or the request is a 404. Each path component is `lstat`-ed; a symlink or a non-directory is refused (403). The file is opened with `O_NOFOLLOW | O_NONBLOCK`, then `fstat` must show a regular file with one link. The resolved path must lie under the root.
- **D3 Streaming.** Each file is read with `os.fdopen(fd, "rb")` and iterated line by line. The header is the first line, read with a byte limit. Listing reads only that header line.
- **D4 Paging.** The cursor is a line number. `GET .../records?after=L&limit=N&types=...` returns records with line > L, and `next_after` for the next page. `seq=N` and `at=<iso>` position the window. Filters are applied while scanning, so a filtered page may scan further than `limit` lines.
- **D5 Activity links.** Activity entries carry `session_id` (which is the transcript run id) and `timestamp`, but no transcript seq and no symphony. The client links `/runs/<session_id>?at=<timestamp>`. The `/runs/<run>` short form is resolved on the server by a scan of the layout, and the page then jumps to the first event at or after that time. The jump is approximate: it is time-based, not seq-based. An exact mapping would need the performer to stamp a seq on every event it reports, which is a change to the performer and is out of scope.
- **D6 Preview.** Any string or JSON value over `PREVIEW_CHARS` (2000) is previewed and flagged `<field>_truncated` with `<field>_bytes`. `GET .../records/{line}/{field}` returns the full value.
- **D7 Summary.** `GET /api/runs/{symphony}/{card}/{run}` streams the file once to compute counts, tokens (run-scope usage if present, else the sum of message-scope usage), duration, outcome (the `result` record) and PR (a GitHub pull URL in the `result` text). A small LRU keyed by `(path, size, mtime_ns)` avoids repeating the scan.
- **D8 Search.** Server-side and streaming: `GET .../search?q=&types=&errors=&after=&limit=` returns matching lines with seq, type and preview. Matching runs over the full text of each record, so nothing is missed in a preview. Jump-to-error uses `errors=1`.
- **D9 Virtualisation.** The client keeps one contiguous loaded window of records (a line range, filtered by type on the server). Rows are measured after render and cached by line, and only the visible range plus a margin is in the DOM. Tool output and full text are fetched when a row is expanded.
- **D10 Registration.** `dashboard_static/runs.js` joins `_DASHBOARD_JS_FILES` before `app`. Styles go in `shell.css`. The page section in `dashboard.html` is markup only.
- **D11 Tokens and cost.** Superseded by #211. The run header takes tokens and cost from the #134 token ledger (`summary.usage`, keyed by run id), with the breakdown and pricing date on hover. A run with no usage records, or a model with no price, says why in place of a figure. The transcript's own usage records are no longer summed in the header.

## Files

- `src/coordinare/services/run_viewer.py` (new): reader, paging, search, summary, downloads, lookup.
- `src/coordinare/dashboard/routers/runs.py` (new): the JSON routes and the page routes.
- `src/coordinare/dashboard/routers/pages.py`: the `/runs/...` page routes.
- `src/coordinare/dashboard/app.py`: registers the router.
- `src/coordinare/mcp/tools/_registry.py`: the deferred prefixes and page paths.
- `src/coordinare/dashboard/dashboard_static/runs.js` (new), `shell.css`, `assets.py`, `app.js`, `performers.js` (card runs, run links), `templates/activity_streams.js` (transcript link), `templates/dashboard.html` (page section).
- Tests: `tests/unit/test_133_run_viewer_api.py`, `tests/unit/test_133_runs_js.py`, `tests/js/runs_checks.js`, `tests/e2e/test_133_transcript_viewer.py`.

## Risks

- The 2,000-event budget depends on the measured window, so the e2e test bounds the DOM size as well as the time.
- Scans of a 50 MB file are linear. The summary cache and the bounded page size keep the request cost flat for the browser.
