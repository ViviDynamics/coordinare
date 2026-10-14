# 132: Full run logs, kept after the pod is gone

**Issue**: ViviDynamics/coordinare#132 (P2, part of #116). Depends on #120 (Kubernetes log decoding, closed). Builds on 129 (run transcripts).

## Problem

The card Live Log polls only the last 200 lines of a performer container's stdout and stderr, every five seconds. When the pod or container is removed, the rest of the run's output is gone. An operator who wants to read the whole run after it ended has nothing.

## Goal

Every containerized performer run keeps its complete stdout and stderr, decoded, redacted and size-capped, in a file the daemon owns. The file can be viewed (paged) and downloaded from the dashboard after the pod has been deleted. The Live Log keeps its tail and gains an "Open full log" link.

## User stories

1. **US1, the full log is kept.** When a run's container is torn down, its complete log is written to disk before the container is removed. Teardown is not delayed beyond a fixed bound, and it still happens if the runtime is unreachable, hangs or raises.
2. **US2, the full log is safe to keep.** The text is decoded as UTF-8, every line is redacted (job secrets by exact value, then the same redactor the activity feed uses), and the file never exceeds the configured cap. When the cap cuts content, the file says how many bytes were dropped.
3. **US3, the full log can be read.** The dashboard serves the log for one run id: a page of text at an offset, or the whole file as a download. Nothing loads the whole file into memory.
4. **US4, the Live Log tail is redacted too.** The tail is the same text, so it is redacted before it is stored, not only before it is shown.

## Requirements

- **FR-001** The full log is collected once per run, at teardown, before the container or pod is removed. Collection streams the runtime's output in bounded chunks and never holds the whole log in memory.
- **FR-002** Collection is bounded in time (`_RUN_LOG_TEARDOWN_SECONDS`). A runtime that hangs, raises or is unreachable never prevents the container from being stopped, and the failure is logged.
- **FR-003** Cancellation during teardown still stops the container. Collection runs inside the existing shielded teardown.
- **FR-004** The log is stored under `run_logs.dir` (default `<state directory>/run-logs`), as `<run_id>.log`. It is never under `performer_log_dir`, which is bind-mounted read-write into every ephemeral performer. A root that overlaps a performer mount, or contains or equals a protected daemon directory, disables run logs for the process (same rule as 129).
- **FR-005** Writes refuse symbolic links and hard-linked files at the run path and at every component below the root. Directories are 0700, files 0600. Writes go to a temporary `.partial` name created with `O_EXCL`, then move into place. An existing final file is never overwritten.
- **FR-006** Each line is decoded from UTF-8 with replacement, then redacted with the job's exact secret values, then `redact_command_text`.
- **FR-007** The cap is `run_logs.max_bytes` (default 16 MiB). The file keeps the first part and the last part of the log, and a marker between them says how many bytes were dropped. The marker is written only when content was dropped.
- **FR-008** Run logs are kept for `run_logs.retention_days` (default 30, the same default as transcripts). The retention sweep runs with the transcript sweep, touches only `<run_id>.log` files directly under the root, never follows links, and never removes a file of a live run or one modified within the last hour.
- **FR-009** The route `GET /api/runs/{run_id}/log` serves the log behind the dashboard's existing authentication. `run_id` must be a single safe path segment (the transcript `sanitize_segment` rule), so no traversal is possible. `offset` and `limit` page the text. `download=1` returns the whole file as an attachment, streamed from disk. A missing or unsafe name is a 404.
- **FR-010** The card Live Log shows an "Open full log" link to the route for the session's run id. The link text and URL are escaped.
- **FR-011** The route is declared as deferred in the MCP registry (`/api/runs`), the same as the other human-dashboard surfaces.

## Out of scope

- Streaming the log during the run (the tail already covers the live view). Collection happens at teardown only.
- Redacting every other dashboard text surface (#203 covers the rest). This spec covers the Live Log tail and the full log file only.
- A pod deleted by someone other than coordinare before teardown: its log cannot be collected by coordinare after that.
