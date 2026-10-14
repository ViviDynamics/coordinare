# Plan: 132 full run logs

## Where it runs

`HTTPPerformerService` removes an ephemeral container in `_cleanup_ephemeral_job` (every path that removes one ends there), and in `_confirm_stopped` for the stop-confirmed path. Capture runs immediately before `PerformerRuntime.stop`, inside the shielded teardown task that already protects the stop from cancellation (`_run_teardown`). `_EphemeralJob` gains `run_id` (the coordinare session id, the same value as the transcript run id) and `log_collected`, so one job collects once.

## Runtime seam

`PerformerRuntime.iter_logs(handle)` is an async iterator of raw output chunks.

- Docker: `docker logs <id>` with stderr merged into stdout, read from the pipe in 64 KiB chunks. The process is killed if the consumer stops early.
- Kubernetes: `read_namespaced_pod_log(..., _preload_content=False)`, then `resp.stream(64 KiB)` in worker threads. The response is closed in a `finally`.

The service probes the method with `getattr`, so runtimes or fakes without it skip capture.

## Storage module (`services/run_log.py`)

- `RunLogWriter`: synchronous, called through `asyncio.to_thread` per chunk. Incremental UTF-8 decoding, line splitting (a line longer than 64 KiB is cut so memory stays bounded), per-line redaction, head-then-bounded-tail accounting. The head is written straight to the temporary file; the tail is a deque capped at about half the budget. On finish the marker is written and the file is renamed into place.
- Path checks reuse `run_transcript._check_chain`, `_refuse_unless_private_regular` and `sanitize_segment`, so the symlink and hard-link rules are the transcript rules.
- `RunLogCollector`: root, enabled flag, cap, retention, ambient secrets; `make_run_logs_collector` applies the same `RootGuard` as transcripts.
- `prune_run_logs`: direct children only, `<run_id>.log` names plus stale `.partial` files.
- `open_run_log` / `read_run_log_window` / `iter_run_log`: validated read helpers used by the route. They open with `O_NOFOLLOW`, require a regular file with one link, and stream from the descriptor.

## Cap and marker

Budget `max_bytes`. The marker reserves 512 bytes. The head budget is half of the rest, and the tail budget the remainder. Marker: `[truncated: N bytes dropped from the middle of this log; kept the first H and the last T bytes]`.

## Tail redaction

`_poll_container_logs` passes each tail line through `redact_text(line, job secrets)` and then `redact_command_text` before storing it in `_log_buffer`. Job secrets are now remembered for every accepted dispatch (previously only for transcribed runs), and a refreshed GitHub token is added to the same list.

## Config

`run_logs:` block (`RunLogsConfig`): `enabled` (true), `dir` (root override), `max_bytes` (16 MiB), `retention_days` (30).

## Wiring

- `__main__._build_run_logs_collector` mirrors `_build_transcript_collector` with the same guard.
- `_build_http_performer_services(..., run_logs=...)` passes the collector to each `HTTPPerformerService`.
- `state["run_log_collector"]` is read by the route and the retention loop.
- `daemon._start_run_log_retention` runs the sweep at start and daily, as the transcript sweep does.

## Dashboard

- `dashboard/routers/run_logs.py` registers `GET /api/runs/{run_id}/log`. Authentication is the existing app-wide middleware.
- `performers.js` `renderCardDetailContent` appends an "Open full log" link when `sess.session_id` is set. The link is built with `esc()` and `encodeURIComponent`.
- `_registry.py` `DEFERRED_REST_PREFIXES` gains `/api/runs`.

## Tests

`tests/unit/test_132_*.py` covers the writer (redaction, cap and marker, symlink and hard-link refusal, root guard, decoding), the runtimes with fakes, teardown ordering, bounds, failure and cancellation, live-tail redaction, the route, and retention. `tests/e2e/test_132_full_run_log.py` covers the link on the card detail.
