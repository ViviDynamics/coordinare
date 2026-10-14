# Feature Specification: Persist a full transcript for every performer run

**Issue**: #129 (part of epic #116, "keep and expose all model output for full transparency")
**Status**: Implemented on branch `129-run-transcripts`; revised after adversarial review (see "Review revisions")
**Related later work**: #130 (run header with the rendered prompt), #131 (conductor calls), #133 (dashboard viewer), #134 (token accounting from `usage` records)

## Problem

Coordinare keeps a short activity log per run. Each backend event is cut to about 200 characters, and the activity log is bounded. Once a performer pod is deleted, the full model output (assistant messages, tool inputs, tool outputs, token usage) is gone. An operator cannot answer "what did the model actually say and do on that card?" after the fact.

## Goal

Every performer run leaves behind one JSON Lines file on the coordinare host. The file holds the complete, untruncated record of what the model and its tools produced, in one format for every backend. The file survives the pod being deleted and a daemon restart. Writing it must never break a run.

## User scenarios

1. **Operator reads a finished run.** A Claude Code run on a card finishes. The operator opens the run's transcript file and finds every assistant message, every tool call with its full input, and every tool result with its full output and error flag, in order.
2. **Pod is gone, daemon restarted.** The performer pod is deleted mid-run or after the run. The daemon is restarted. The transcript file is still there, complete up to the last record the daemon received, and a restarted daemon continues the same file for a run still in flight.
3. **Backend cannot provide everything.** A backend that only reports coarse progress text does not pretend to have tool records. Its transcript says, in one `capability` record, which record types that backend cannot provide.
4. **Secrets never reach the file.** Credentials in tool input, tool output, assistant text, JSON-encoded strings, `NAME=value` lines and structured keys are written as `[REDACTED]`. So are the job's own secret values, matched exactly.
5. **Storage is bounded.** A run that produces more than the configured byte cap gets one `truncated` marker and then stops writing content, so the marker explains the gap.
6. **Performers cannot reach the transcripts.** The transcripts folder is never inside a directory mounted into a performer container. A container for one card cannot read or rewrite another card's transcript.
7. **Old transcripts expire.** Transcripts older than the retention period are deleted automatically. The sweep never touches files outside the transcripts folder.
8. **Operator turns it off.** With `transcripts.enabled: false`, no transcript traffic is added to the status poll, no files are written, and nothing on disk is created or swept.
9. **A broken transcript never breaks a run.** A full disk, a bad path, a planted symlink or a malformed record fails that run's transcript only. Status polling, dispatch, teardown and the backend's event reader continue.

## Functional requirements

- **FR-001 Location.** Each performer run writes exactly one JSONL file at `<transcripts root>/<symphony>/<card>/<run_id>.jsonl`. The transcripts root defaults to `<state directory>/transcripts`, a daemon-only area beside the state store. `transcripts.dir` overrides it; a relative override is taken from the config directory. This deliberately departs from the issue's literal path (`<performer_log_dir>/transcripts`), because `performer_log_dir` is bind-mounted read-write into every ephemeral performer container.
- **FR-002 Mount safety.** At startup the root must not be inside, and must not contain, any host directory a performer can see: `performer_log_dir` and every configured performer volume `host_path`. If it does, transcription is disabled for the process and an error is logged (`run_transcript.root_mounted_into_performer`). The daemon keeps running. Transcripts are diagnostics, and refusing to start would stop the orchestrator over an optional setting.
- **FR-003 Record shape.** Every line is one record with `ts`, `run_id`, `seq`, `type`, plus the optional fields `text`, `tool_name`, `call_id`, `input`, `output`, `is_error`, `usage` and `scope`. `type` is one of `system`, `user`, `assistant_text`, `thinking`, `tool_call`, `tool_result`, `usage`, `result`, `error`, `capability`, `truncated`.
- **FR-004 Run identity.** `run_id` is the coordinare session id the daemon already allocates per dispatch. No second identifier is introduced. The performer uses the same value as its job id.
- **FR-005 Sequence.** `seq` is a contiguous 1-based sequence per run and the dedupe key. See FR-016 for how the daemon validates it.
- **FR-006 Full content.** Content is never truncated. `text`, `input` and `output` carry the full value. Only the size bounds in FR-017 apply.
- **FR-007 Claude Code.** Every stream-json event type is mapped: system init and other system events, user prompt, assistant text, thinking, every `tool_use` with full input, every `tool_result` with full output and `is_error`, per-message usage, the run total, and errors. A `stream_event` (emitted with `--include-partial-messages`) and any unknown type are recorded as a `system` record carrying the raw type name and payload. Nothing is silently dropped.
- **FR-008 Other backends.** Every other backend maps what its output stream provides. A backend that cannot provide a record type declares it in one `capability` record per run. A backend never fills a field it cannot know.
- **FR-009 Redaction scope.** Records are redacted in the performer before any batch leaves it (at serve time, on a worker thread), and again in the daemon before any line is written. Redaction covers:
  - values under credential-named keys, matched case-insensitively as substrings (`token`, `secret`, `password`, `passwd`, `api_key` / `api-key` / `apikey`, `authorization`, `credential`, `private_key`, `cookie`), applied recursively to dicts and lists. `token` excludes plural counts (`input_tokens`, `max_tokens`), so usage numbers are kept;
  - `NAME=value`, `NAME: value` and `"NAME": "value"` forms whose name matches those stems;
  - strings that hold JSON, parsed and redacted inside;
  - value shapes: GitHub `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_` and `github_pat_`; Anthropic `sk-ant-`; LiteLLM `sk-litellm-`; OpenAI `sk-proj-` and generic `sk-`; AWS `AKIA`; `Authorization:` header values; `Bearer` tokens; Slack webhooks;
  - exact values of every secret the job was handed (performer side: the job payload secrets and values PATCHed in mid-run; daemon side: the secrets it sent at dispatch, refreshed tokens, and credential-named environment variables of the daemon process).

  The two copies of the shared rules are byte-identical, enforced by a test. `lib/redaction.py` is not changed.
- **FR-010 Delivery.** Delivery is incremental. On every status poll the daemon asks for the records after its cursor. The performer returns a bounded batch (default 200 per poll, at most 500 per request, and at most 8 MiB of serialized output, with at least one record always returned). The response also reports the performer's last seq.
- **FR-011 Terminal and teardown drains.** Before a finished container is removed, the daemon drains every remaining record, under a 30 second wall-clock bound. Before a cancelled, stopped, reaped or unreachable container is removed, it makes a best-effort drain under a 5 second bound. A drain never delays a teardown beyond its bound and never prevents one.
- **FR-012 Byte cap.** `max_bytes_per_run` caps one run's file. When the next content record would exceed the cap, exactly one `truncated` marker is written and later content records are counted, not written. The performer buffer enforces the same cap.
- **FR-013 Retention.** `transcripts.retention_days` deletes `.jsonl` files older than the period. `0` keeps transcripts forever. The default is 30. The sweep runs once at daemon start and then daily, in a worker thread, outside the cycle loop. It deletes only regular files inside the root, never follows symlinks, and removes folders it leaves empty. When transcripts are disabled, the sweep does not run.
- **FR-014 Disabled.** `transcripts.enabled: false` means the status poll carries no transcript parameters, the job payload carries no cap, the performer creates no buffer, and the daemon creates no directory, no file and no probe. Nothing on disk is touched.
- **FR-015 Older performers.** An older performer image that returns no transcript fields is reported once per run (`run_transcript.performer_unsupported`). Nothing is written for that run, and the run itself is unaffected.
- **FR-016 Sequence validation.** The daemon accepts a record only when `cursor < seq <= cursor + (number of records in the batch)`, and `seq <= transcript_last_seq` from the same response. Seq must strictly increase within a batch. A record that fails any test is logged (`run_transcript.seq_rejected`) and skipped, and the cursor does not advance past it. One record with a huge seq therefore cannot hide later records.
- **FR-017 Size bounds.** A single record larger than 16 MiB, as written, becomes a `truncated` marker (the same single-marker rule as FR-012). The daemon accepts at most 32 MiB of records per batch. A batch cut short is requested again on the next poll, and at least one record is always accepted, so delivery progresses.
- **FR-018 Usage scope.** Every `usage` record carries `scope`. `"message"` is the usage of one model message, and a message's usage is written once, with the last chunk's counts. `"run"` is a run or cumulative total. A consumer that sums usage counts only one scope. Where a backend's contract does not say whether a figure is per message or per run, `scope` is absent, and the record says so in `text`.
- **FR-019 Best effort.** Transcript code never raises into status polling, dispatch, teardown, retention or a backend's event reader loop. Each capture point catches its own failures. The first failure for a run is logged once (`run_transcript.capture_failed` or `run_transcript.run_failed`) and the run's transcript is marked failed. Later writes for that run are skipped. The run itself is unaffected.
- **FR-020 Safe paths.** The writer never follows a symlink for the run file or any directory between the root and the file. It opens files with `O_NOFOLLOW`, checks each directory with `lstat`, and refuses (logging `run_transcript.run_failed`) anything that is not a plain directory or regular file. A refusal writes nothing and never raises.

## Success criteria

- **SC-001** A Claude Code run yields a transcript with every assistant text block, every tool call with its full input, and every tool result with its full output. Verified by a unit test that drives the real stream-json handler with a 120 KB tool output.
- **SC-002** The transcript is complete after the daemon restarts mid-run: a second writer over the same file resumes from the file's last seq. Verified by a test.
- **SC-003** The secrets test asserts on the bytes on disk. It covers GitHub tokens (all `gh*_` prefixes and `github_pat_`), `Authorization: Bearer` and `Authorization: Basic`, Anthropic, OpenAI-project and AWS shapes, a credential-named key in several spellings, a `password` inside a JSON string, a `NAME=value` env dump, and an exact job secret that matches no pattern.
- **SC-004** Retention removes a transcript older than the period, keeps a recent one, and does not touch files outside the root. Verified by a test.
- **SC-005** With transcripts disabled, the status request has no transcript query parameters and nothing is created on disk. Verified by a test.
- **SC-006** A planted symlink at a directory or the run file never redirects a write, never raises, and leaves the target untouched. Verified by tests.
- **SC-007** A root inside a performer mount disables transcription with an error log. Verified by a test.
- **SC-008** A failing writer does not break a status poll or a teardown, and a hanging drain does not delay a teardown beyond its bound. Verified by tests.

## Second review revisions

A second adversarial review found further defects. These revisions change the following behaviour:

- Redaction is linear. A regex that retried from every position of a long word run was quadratic (1 MB of alphanumerics took well over a second at 24 KB). Assignments are now found by tokenising words once and matching separators and values at fixed positions. A 1 MB word redacts in tens of milliseconds, and a test asserts it (FR-009).
- Redaction covers escaped JSON inside strings, URL userinfo passwords, whole PEM private key blocks, and unquoted `=` values to the end of the line. Usage keys such as `tokenizer` and `tokens` are not credential stems, because stems match whole words. The prose case `token: the cache is stale` redacts one word by design: redacting an unquoted `:` value to the end of the line would hide `Authorization: Basic xyz`.
- File I/O is off the event loop. The writer does no I/O at construction. The cursor is read from the file's last line only, on a worker thread, before the first poll. Batches are written on a worker thread, under a per-run lock. On the performer, redaction runs while a batch is served, on a worker thread, so the backend and the status endpoint are not blocked by regex work.
- A file reachable through a second hard link is refused, both when its cursor is read and when it is appended to (FR-020).
- Retention only considers the exact layout the writer creates (`<root>/<symphony>/<card>/<run>.jsonl`), with names the writer could have produced, and never removes a live writer's file.
- `transcripts.dir` is validated: `~` is expanded, and the root may not equal or contain the state directory, the config directory, the attempt log directory, or an env-cache root. It may not lie inside or contain a performer mount. Comparisons are case-insensitive on case-insensitive filesystems, detected by probing. A run whose per-symphony env-cache mounts overlap the root is not transcribed.
- Per-run daemon state is released on every terminal path, including system-error teardown (which goes through the ephemeral cleanup). Secrets, targets and markers are dropped. A finished run is remembered, in a bounded set, so a late poll or release never recreates its writer. A cursor ahead of the performer's last seq is logged (`run_transcript.cursor_ahead_of_performer`).
- A message without an id is written once, with its last chunk's usage.
- With transcripts disabled, the dispatch payload is exactly the pre-129 payload. The session id is not copied into the job id.

Deployment note: the default root is `<state directory>/transcripts`. `docker-compose.yml` mounts only `config.yaml` into the coordinare container, so the state directory, and with it the default transcripts root, lives in the container layer and is lost when that container is recreated. Point `transcripts.dir` at a mounted volume that is outside every performer mount to keep transcripts across container recreation. No compose change is made in this issue.

## Third review revisions

A third review, of the branch as PR #174, found the following. The changes are in code and tests, and the behaviour is:

- **A record that cannot be redacted never fails a poll.** Redaction recursion is bounded (depth 64). A structure that cannot be redacted structurally falls back to pattern redaction of its text, and a non-text value is replaced by a marker. The performer's batch serving is guarded per record: a record that cannot be redacted is served as a withheld `error` record with the same seq. A failure in serving the batch returns the normal status with an empty batch and `transcript_error` set, so completion is still observed. The daemon writes a withheld marker for one bad record and continues with the run.
- **Lines are always valid JSON.** Redaction changes string values only, and the line is never rewritten. Every line keeps its seq. A property-style test checks every string field against a corpus of nasty strings. An unparsable last line no longer resets the cursor: the scan goes back to the last line that parses.
- **File I/O cannot hang a worker.** The run file is opened non-blocking and must be a regular file before any write, so a FIFO at the path fails the run. A failed load never reaches a write. Writes complete short writes, and a failed write truncates back to the pre-batch size, so no torn line stays mid-file.
- **Completion never waits on the transcript.** The terminal state is returned at once. The drain (bounded at 30 seconds) and the teardown run in a background task, and the teardown always follows the drain. Each poll waits at most 2 seconds for a transcript operation. A slow cursor read falls back to a plain poll, so no duplicate is possible.
- **Code is not redacted as if it were a secret.** A comparison (`==`, `===`), a type annotation (`token: str`), a bare number or boolean, and a name with a descriptive suffix (`_count`, `_limit`, `_budget`, `_usage`, `_type`, `_name`) are left alone. A PEM block is redacted from BEGIN to its matching END. A BEGIN with no END is redacted only when base64 lines follow it.
- **More credential shapes are caught**: netrc `password` (on a machine line, or alone on a line), `curl -u user:password` and `--user`, bare JWTs, Stripe `sk_live_` and `rk_live_` keys, PGP private key blocks, the `pass_word`, `passphrase` and `*_API_KEY` names. ANSI escape sequences are removed before matching, so they cannot hide a secret. Matching stays linear: 5 MB inputs take about half a second.
- **Retention races and shutdown.** A folder that a writer re-creates while the sweep removes it is not an error. The sweep never removes a file modified within the last hour. The sweep loop exits on shutdown as well as on cancellation.
- **Names and directories.** A changed sanitised name carries a short digest, so two distinct ids never share a file. New directories are created private. A batch that arrives out of order is sorted and written whole.
- **Record size.** A string field longer than 256 KiB is cut with a marker on the performer before it is buffered. A record still over 1 MiB has its input and output withheld, so a single record cannot approach the per-run cap or the 30-second client timeout.
- **Lone surrogates** are replaced in text before redaction, so the output is valid text and nothing is dropped.

Documented limits that are unchanged: base64, URL-encoded or split secrets are not matched; exact-value matching applies to values of 8 or more characters; two daemons writing the same file are not supported.

## Known loss window

Records are pulled on each status poll. A pod that dies on its own between two polls loses up to one poll interval of records. The file keeps everything pulled before that. Drains before cancellation, reaping and normal termination narrow this window, but cannot close it when a pod is deleted by the platform without warning.

## Review revisions

The first implementation was reviewed adversarially. The revisions above change the following behaviour, relative to that first version:

- The default root moved from `performer_log_dir/transcripts` to `<state directory>/transcripts` (FR-001, FR-002). Kubernetes pods never mount either directory: the Kubernetes runtime mounts only devenv cache claim subpaths.
- Redaction now covers credential-named keys, env lines, embedded JSON, more token shapes and exact job secrets (FR-009).
- Seq validation and size bounds were added (FR-016, FR-017).
- Teardown drains were added (FR-011). The loss window is stated above.
- The usage model changed: last chunk per message, explicit `scope` (FR-018).
- Capture is best effort (FR-019), and the writer refuses symlinks (FR-020).
- Disabled mode touches nothing, including retention (FR-014).
- `max_bytes_per_run` and `retention_days` have bounds, and `transcripts.dir` was added.

## Assumptions and scope

- The record schema is shared across backends. Field names are fixed here. Later issues (#130, #131, #133, #134) build on this file format and must not change existing record meanings.
- `text` holds prose (assistant text, thinking, user prompts, error messages, the capability reason). `input` and `output` hold structured or large payloads. `call_id` correlates tool calls with results.
- Transcripts are written for runs that go through the containerized performer transport (ephemeral and persistent, Docker or Kubernetes). The development-only subprocess transport is not covered (see plan.md).
- The default `max_bytes_per_run` is 50 MiB and the default retention is 30 days.
- Dashboards, run headers and token totals are out of scope for this issue.
