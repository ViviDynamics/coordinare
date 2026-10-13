# Feature Specification: Persist a full transcript for every performer run

**Issue**: #129 (part of epic #116, "keep and expose all model output for full transparency")
**Status**: Implemented on branch `129-run-transcripts`
**Related later work**: #130 (run header with the rendered prompt), #131 (conductor calls), #133 (dashboard viewer), #134 (token accounting from `usage` records)

## Problem

Coordinare keeps a short activity log per run. Each backend event is cut to about 200 characters, and the activity log is bounded. Once a performer pod is deleted, the full model output (assistant messages, tool inputs, tool outputs, token usage) is gone. An operator cannot answer "what did the model actually say and do on that card?" after the fact.

## Goal

Every performer run leaves behind one JSON Lines file on the coordinare host. The file holds the complete, untruncated record of what the model and its tools produced, in one format for every backend. The file survives the pod being deleted and a daemon restart.

## User scenarios

1. **Operator reads a finished run.** A Claude Code run on a card finishes. The operator opens `<performer_log_dir>/transcripts/<symphony>/<card>/<run_id>.jsonl` and finds every assistant message, every tool call with its full input, and every tool result with its full output and error flag, in order.
2. **Pod is gone, daemon restarted.** The performer pod is deleted mid-run or after the run. The daemon is restarted. The transcript file is still there, complete up to the last record the daemon received, and a restarted daemon continues the same file for a run still in flight.
3. **Backend cannot provide everything.** A backend that only reports coarse progress text does not pretend to have tool records. Its transcript says, in one `capability` record, which record types that backend cannot provide.
4. **Secrets never reach the file.** A token that appears in a tool argument, a tool output, or assistant text is written as `[REDACTED]`.
5. **Storage is bounded.** A run that produces more than the configured byte cap gets one `truncated` marker and then stops writing content, so the marker explains the gap.
6. **Old transcripts expire.** Transcripts older than the retention period are deleted automatically. The sweep never touches files outside the transcripts folder.
7. **Operator turns it off.** With `transcripts.enabled: false`, no transcript traffic is added to the status poll and no files are written.

## Functional requirements

- **FR-001** Each performer run writes exactly one JSONL file at `<performer_log_dir>/transcripts/<symphony>/<card>/<run_id>.jsonl`. When `performer_log_dir` is unset, the transcripts root is `<config directory>/logs/transcripts`, falling back to the state directory when the config directory is read-only (the same rule the attempt log follows).
- **FR-002** Every line is one record with the fields `ts`, `run_id`, `seq`, `type`, plus the optional fields `text`, `tool_name`, `call_id`, `input`, `output`, `is_error`, `usage`. `type` is one of `system`, `user`, `assistant_text`, `thinking`, `tool_call`, `tool_result`, `usage`, `result`, `error`, `capability`, `truncated`.
- **FR-003** `run_id` is the coordinare session id that the daemon already allocates for each dispatch. No second run identifier is introduced. The performer uses the same value as its job id, so the two ids agree.
- **FR-004** `seq` is a contiguous 1-based sequence per run. It is the dedupe key: a record whose seq is at or below the file's last written seq is never written twice.
- **FR-005** Content is never truncated. `text`, `input` and `output` carry the full value the backend received or produced.
- **FR-006** Claude Code maps every stream-json event type it emits: system init and other system events, user prompt, assistant text, thinking, every `tool_use` with full input, every `tool_result` with full output and `is_error`, per-message usage (input, output, cache read, cache write, model), the final result, and errors.
- **FR-007** Every other backend maps the events its own output stream actually provides. A backend that cannot provide a record type declares it in exactly one `capability` record per run, listing the unavailable types and the reason. A backend never fills a field it cannot know.
- **FR-008** Redaction runs before anything is stored in the performer and again before a line is written by the daemon. Token patterns and sensitive keys (`token`, `password`, `secret`, `api_key`, `authorization`, and similar) are replaced with `[REDACTED]` or `***REDACTED***`.
- **FR-009** Delivery is incremental. The daemon asks the performer for the records after its cursor on every status poll. The performer returns a bounded batch (default 200, at most 500 per request). The response also reports the performer's last seq, so the daemon knows whether more records are pending.
- **FR-010** Before a finished run's container is torn down, the daemon drains every remaining record.
- **FR-011** `max_bytes_per_run` caps one run's file. When the next content record would exceed the cap, exactly one `truncated` marker is written and later content records are counted, not written. A marker already in the file is never repeated after a restart.
- **FR-012** `transcripts.retention_days` deletes `.jsonl` files older than the period. `0` keeps transcripts forever. The sweep runs once at daemon start and then daily, in a worker thread, outside the cycle loop. It deletes only regular files inside the transcripts root, never follows symlinks, and removes folders it leaves empty.
- **FR-013** `transcripts.enabled: false` means the status poll carries no transcript parameters and the job payload carries no cap, so the performer records nothing. No file is created.
- **FR-014** The file survives a pod deletion once the daemon has pulled its records, and a daemon restart. A new daemon process reads the last seq from the file and resumes appending to the same file without duplicates or gaps.
- **FR-015** An older performer image that does not return transcript fields is reported once per run (`run_transcript.performer_unsupported`). Nothing is written for that run and the run itself is unaffected.

## Success criteria

- **SC-001** A finished Claude Code run yields a transcript that contains every assistant text block, every tool call with its full input, and every tool result with its full output, verified by a unit test that drives the real stream-json handler with a 100 KB tool output.
- **SC-002** The transcript is complete after the daemon restarts mid-run: a second collector over the same directory resumes from the file's last seq. Verified by a test.
- **SC-003** A secrets test shows GitHub tokens (`ghp_`), `Authorization: Bearer` headers, Anthropic-shaped keys and AWS-shaped keys written as `[REDACTED]`, in tool input, tool output and assistant text, on both sides.
- **SC-004** Retention removes a transcript older than the period, keeps a recent one, and does not touch files outside the root. Verified by a test.
- **SC-005** With transcripts disabled, the status request has no transcript query parameters and no file is created. Verified by a test.

## Assumptions and scope

- The record schema is shared across backends. Field names are fixed here. Later issues (#130, #131, #133, #134) build on this file format and must not change existing record meanings.
- `text` holds prose (assistant text, thinking, user prompts, error messages, the capability reason). `input` and `output` hold structured or large payloads. The issue's field list is kept, with `text` and `call_id` added so prose and tool-call correlation have a home.
- Transcripts are written for runs that go through the containerized performer transport (ephemeral and persistent, Docker or Kubernetes). The development-only subprocess transport is not covered (see plan.md, "Not covered").
- The performer buffer lives in memory for the life of the job. It is bounded by the same byte cap the daemon uses, so memory use is bounded by `max_bytes_per_run`.
- The default `max_bytes_per_run` is 50 MiB and the default retention is 30 days.
- Dashboards, run headers and token totals are out of scope for this issue.
