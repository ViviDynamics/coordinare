# Plan: Persist a full transcript for every performer run

Implements `spec.md`. This file records the design, the decisions behind it, and the revisions made after adversarial review.

## Architecture

Two packages, one contract. The performer runs in a container or pod that is deleted after the run. The daemon owns the file.

```
backend (claude_code, codex, ...)
   |  transcript.emit(type, text=..., input_data=..., scope=...)   performer/transcript.py  (best effort)
   v
TranscriptBuffer (per job, in the job runner)                       seq-numbered, byte-capped, redacted
   ^
   |  GET /jobs/{id}?transcript_after=N&transcript_limit=L
   |  -> JobStatus.transcript (records seq > N, <= 8 MiB), JobStatus.transcript_last_seq
   |
HTTPPerformerService (daemon)                                       coordinare/services/http_performer_service.py
   |  RunTranscriptWriter.append_batch(records, last_seq)            coordinare/services/run_transcript.py (never raises)
   |     seq window, size bounds, redaction, O_NOFOLLOW
   v
<state dir>/transcripts/<symphony>/<card>/<run_id>.jsonl            (default root, see D5)
```

The performer never imports coordinare, and coordinare never imports the performer. The record models and the redaction module are mirrored by copy. Mirroring is enforced by tests.

## Decisions

### D1. Full-fidelity channel separate from `BackendEvent`

`BackendEvent.text` is capped at 200 characters and `drain_events()` feeds the activity log. Transcripts must not reuse that channel. Each backend emits transcript records at its raw parse site and leaves its existing `_emit` calls unchanged.

### D2. Run identity is the existing coordinare session id

`_dispatch_card_locked` allocates `session_id = uuid4()` before the container starts. The job payload's `job_id` is set to that value, so the performer's record `run_id`, the file name and the daemon's run identity agree. In persistent mode they were already equal. The daemon overwrites `run_id` on write.

### D3. Delivery: pull by cursor, drain before every teardown

Rejected alternative: push from the performer. The daemon already polls, and a push would need a new inbound endpoint.

- Request: `GET /jobs/{id}?transcript_after=N&transcript_limit=L`. The query is sent only when transcripts are enabled.
- Response (additive): `transcript` and `transcript_last_seq`.
- Batch bounds: the performer returns at most `L` records (route clamp 1 to 500) and at most 8 MiB serialized, always at least one record. The daemon accepts at most 32 MiB per batch and at most 16 MiB per record (D10).
- Cursor: the highest seq written, recovered from the file on writer creation. No separate store, no schema change.
- Teardown drains (review revision, MEDIUM 6): before the terminal cleanup, a 30 second drain runs. Before `release_session`, `stop_session_confirmed` and the transport-error cleanup, a 5 second best-effort drain runs. Both use `asyncio.timeout`, so the bound is wall-clock, not a round count. The 5000-round limit remains as a second guard.
- Loss window: records pulled on the last poll are safe. A pod that dies on its own between polls loses up to one poll interval (spec, "Known loss window").

### D4. Redaction (review revision, HIGH 4)

One module, `transcript_redaction.py`, copied byte-identically to `lib/transcript_redaction.py` (daemon) and `performer/transcript_redaction.py` (performer). A test fails if the copies differ, and another checks that the pattern lists match.

Rules, in order, on each string:

1. exact values from the secret provider, longest first, minimum length 8;
2. value shapes (`VALUE_PATTERNS`), including `gh[pousr]_`, `sk-ant-`, `sk-litellm-`, `sk-proj-`, generic `sk-`, `AKIA`, Bearer and Slack;
3. the `Authorization:` header value, to end of line;
4. `NAME=value`, `NAME: value` and `"NAME": "value"` where NAME matches the stems `token(?!s)`, `secret`, `passw(or)d`, `api[_-]key`, `authorization`, `credential`, `private[_-]key`, `cookie` (case-insensitive substring).

Structured values: a dict value under a stem-matching key is replaced whole. Dict keys are also redacted as text. A string that parses as a JSON object or array is redacted inside and re-encoded. `token(?!s)` keeps counts such as `input_tokens` and `max_tokens`.

Placement (second review revision, item 2): the performer stores records raw and redacts in `TranscriptBuffer.after()`, when a batch is served. The serving route runs that work on a worker thread, so it never runs on the loop that drives the backends. The secret provider is read at that time, so mid-run refreshes apply to every record served afterwards. Records held in the performer's memory are raw; nothing leaves the performer unredacted. The daemon redacts in `RunTranscriptWriter._prepare` with the same rules (exact values from the dispatch payload, refreshed tokens, and the daemon process's credential-named environment), then runs `redact_text` on each serialized line as a last pass.

Second review (items 1 and 5): the rules are linear. Assignments are found by tokenising words, not by a regex with a leading class retried at each position. Credential stems match whole words (`(?![a-z])`), so `tokenizer` and `tokens` are not stems. Additional rules: escaped JSON inside a string (`\"KEY\": \"value\"`), URL userinfo (`scheme://user:password@host`, password redacted), whole PEM private key blocks, and unquoted `=` values to end of line. The prose case `token: the cache is stale` redacts one word by design (see the comment in `_redact_assignments`).

Known limit: the daemon does not hold its minted GitHub App installation token in an environment variable. Exact-value redaction of that token covers only the values the daemon sent at dispatch and the refreshed values the monitor passes on each poll.

### D5. Location (review revision, HIGH 1)

Default root: `<state directory>/transcripts`, beside the state store. Not `performer_log_dir`.

Why not the issue's literal path: `performer_log_dir` is bind-mounted read-write into every ephemeral container at `/var/log/performer` (`__main__.py`, `performer_lifecycle.py`). A root inside it would let one container read and rewrite another card's transcript.

Override: `transcripts.dir` (YAML key `dir`). A relative value is taken from the config directory.

Startup check (`make_collector`): the root is refused if it lies inside, or contains, any directory in `_performer_mount_hosts(config)`: `performer_log_dir` plus each performer endpoint's `volumes[].host_path`. On a match, transcription is disabled for the process and `run_transcript.root_mounted_into_performer` is logged at error level. The daemon keeps running, because transcripts are diagnostics and refusing to start would stop the orchestrator.

Kubernetes (verified by reading `services/kubernetes_runtime.py`, lines 230 to 270): pods mount only the `config.volumes` entries whose container path is under the devenv root, as subpaths of the devenv cache PVC. The log dir and the state dir are not mounted. The `VolumeMount` for the log dir exists in config for Kubernetes too, but the runtime drops it, because `/var/log/performer` is outside the devenv root. The startup check still applies, because the same config drives both runtimes.

Path safety (see D11): segments are sanitized, and the writer refuses symlinks.

### D6. Symlink and file-system hardening (review revision, HIGH 2)

- `transcript_path` is a lexical join. It no longer calls `resolve()`, so a planted link is seen by the writer rather than silently followed or turned into a `ValueError`.
- The root is resolved once when the collector is built, and is trusted.
- Each directory below the root is checked with `os.lstat` before use. A symlink or non-directory is refused.
- The run file is opened with `O_NOFOLLOW` (`O_WRONLY|O_APPEND|O_CREAT` for writes, `O_RDWR` for the cursor read and torn-line truncation), and its `fstat` must show a regular file.
- A refusal raises an internal error that `append_batch` catches. It marks the run failed, logs once, and returns 0.

### D7. Best-effort capture (review revision, HIGH 3)

Every boundary catches broadly, logs once per run or once per site, and continues:

- Performer: `emit()` and every backend transcript helper are decorated with `@best_effort`. The decorator logs `transcript.capture_failed` once per call site and returns None. Helpers that also drive backend state (`_fail_start`, `_fail_spawn`, the reader loop itself) are deliberately not decorated, because a decorator would swallow real errors.
- Daemon: `writer_for` and `RunTranscriptWriter` never raise (the constructor and `append_batch` contain their own failures). `_absorb_transcript`, `_drain_transcript`, `_transcript_writer` and `_teardown_drain` catch and call `_transcript_failed`, which marks the writer failed and logs once per run.
- Collector construction in `__main__` catches and returns None. The retention loop catches everything and keeps running.
- Cleanup order: the terminal drain is contained, so `_cleanup_ephemeral_job_by_id` always runs after it.

### D8. Seq validation and size bounds (review revision, MEDIUM 5)

For one batch, with `base` the cursor at the start and `window_end = base + len(batch)`:

- a record is skipped if `seq <= cursor` (a re-delivery);
- otherwise it is rejected unless `base < seq <= window_end` and `seq <= transcript_last_seq`;
- seq must strictly increase within the batch;
- a rejected record is logged (`run_transcript.seq_rejected`) and does not move the cursor.

Size: a record over 16 MiB, as written, is replaced by a `truncated` marker, following the single-marker rule. A batch stops once 32 MiB is accepted, and the rest is re-requested. The first record of a batch is always accepted, so progress is guaranteed.

Response size: the daemon's HTTP client reads the whole response before the writer sees it, so the bound is on what the daemon writes and on the performer's own output (8 MiB per batch, plus at most one record). The daemon does not stream-parse the body.

### D9. Usage scope (review revision, MEDIUM 7)

- `scope` is a field on every record: `"message"` or `"run"`, or absent when the backend's contract does not say.
- Claude Code: the assistant event's usage is the message usage. Each message id keeps the last usage seen, which replaces earlier chunks. The record is written when the message ends: when a different message id arrives, at the result event, or when the reader exits. The result event's totals are written with `scope: "run"`. Test: the first chunk reports 5 output tokens and the last reports 40; the record must carry 40, and the run total is separate.
- Codex: the thread's cumulative total at each turn end is `scope: "run"`.
- Junie: the result file's totals are `scope: "run"`.
- Pi: per-call counts are `scope: "message"`.
- Driver: the result line's totals are `scope: "run"`. Cost events do not say which, so their scope is absent.

Verification by reading: the repo's Claude fixtures show usage on assistant events, and `claude_code.py` passes `--include-partial-messages`, whose `stream_event` payloads carry `message_delta` usage. The repo has no captured fixture that shows a later assistant chunk with a higher output count. The last-chunk rule is safe either way: it never undercounts, and a later higher count wins.

### D10. Stream events and unknown types (review revision, LOW 11)

`claude_code.py` passes `--include-partial-messages`, so `stream_event` records really arrive. Every event type not handled by name is recorded as a `system` record with the text `unhandled stream event type: <type>` and the raw event as `input`. Nothing is dropped.

### D11. Paths and identifiers

`sanitize_segment` keeps `[A-Za-z0-9._-]`, replaces everything else with `_`, strips leading and trailing `._`, caps the length at 120, and maps an empty result to `unattributed`. `transcript_path` joins those segments lexically.

### D12. Symphony and card attribution

The monitor passes `transcript_target: {symphony, card_id}` in the status payload on every poll: in `phase_458` and in the observer stall probe. The service remembers the target for the run. A poll with no target writes to `unattributed/unattributed/<run_id>.jsonl`.

### D13. Disabled mode (review revision, LOW 8)

With `enabled: false`: no buffer, no cap in the payload, no query parameters on the status request, no writer, no directory, no probe, and no retention sweep. `make_collector` does not touch the disk. `resolve_transcripts_root` is pure.

### D14. Config (review revisions, LOW 9)

`TranscriptsConfig` (`extra="forbid"`, `populate_by_name=True`): `enabled` (default true), `dir` (alias of `directory`, default none), `retention_days` (default 30, 0 to 3650), `max_bytes_per_run` (default 50 MiB, 1 KiB to 1 GiB).

### D15. Second review: I/O, links, retention, validation, release

- **I/O off the loop.** `RunTranscriptWriter` does no I/O at construction, so `writer_for` is memory-only. `ensure_loaded()` reads the cursor from the file's tail: the last complete line holds the highest seq, and a cap marker is always the last line, so the file is never read whole. The tail is read in 64 KiB chunks backward. Both `ensure_loaded` and `append_batch` run on a worker thread (`asyncio.to_thread`), under a per-run `threading.Lock`. The service awaits `ensure_loaded` before it first reads the cursor.
- **Hard links.** `os.fstat` must report `st_nlink == 1` before the file is truncated (torn line) or appended to. The check runs on the descriptor, so a path swapped after `lstat` cannot defeat it.
- **Retention layout.** `prune_expired` walks exactly three levels: `<root>/<symphony>/<card>/<run>.jsonl`. A directory counts only if `sanitize_segment(name) == name`, and a file only if its stem is a sanitized name. Symlinks and anything at another depth are never touched. Files in `keep` (live writers' paths, from `TranscriptCollector.live_paths()`, read on the loop) are skipped.
- **Root validation.** `RootGuard(performer_mounts, protected)`. Performer mounts are the log dir, endpoint volume host paths, and the env-cache root. A root inside a mount, or containing one, is refused. Protected directories (state, config, attempt log) are refused when the root equals or contains them. Inside them is allowed, because the default root is inside the state directory. Comparison is case-insensitive when `_case_insensitive` says the filesystem is, probed by swapping the case of an existing name and asking the filesystem whether it is the same file. Per-run env-cache mounts are checked at dispatch (`_transcript_identity`): an overlapping run gets no cap and no writer.
- **Release.** `_release_transcript_run(session_id)` drops secrets, targets, failure and support markers, and refusal markers, and releases the writer. It runs on the terminal branch, in `_cleanup_ephemeral_job_by_id` (which every container teardown reaches, including system-error teardown), and in the teardown drain's `finally`. The finished run id is kept in a dict bounded at 4096. `_transcript_writer` returns None for a finished run, so a late poll or release never recreates a writer, and never creates `unattributed/`.
- **Disabled payload.** With transcripts disabled, `_transcript_identity` returns the payload object unchanged. Dispatch is byte-identical to the pre-129 path.
- **Claude Code usage without a message id.** Chunks with no id are keyed by `None` and replace one another, so only the last is written.

## Wire and contract changes

| Surface | Change | Compatibility |
|---|---|---|
| Performer `JobInitPayload` | `transcript_max_bytes: int \| None = None` | older performers ignore unknown fields |
| Coordinare `JobInitPayload` | same field, omitted when `None` | `None` is omitted from the wire |
| Performer and coordinare `JobStatus` | `transcript: list`, `transcript_last_seq: int \| None` | defaults empty |
| `GET /jobs/{id}` | optional `transcript_after` (ge 0), `transcript_limit` (1 to 500) | absent params keep today's response |
| `PerformerHTTPClient.get_job` | optional keyword arguments | default call unchanged |

The `protocol.py` mirrors are not changed. The stdio status path forwards the observer probe's `transcript_target` payload key unchanged (test `test_129_redaction_parity.py`, LOW 13).

## Per-backend capability table

"Provided" means the backend's own output carries the information. "Unavailable" is what goes into the `capability` record. Each row was checked by reading the parse code and by tests that drive it. None of the non-Claude backends was run against a live binary.

| Backend | Records emitted | Declared unavailable | Usage scope | How it was established |
|---|---|---|---|---|
| `claude_code` | `system` (init, other subtypes, `stream_event`, unknown types), `user`, `assistant_text`, `thinking`, `tool_call`, `tool_result` (paired by `tool_use_id`), `usage` (message and run), `result`, `error` | none. `thinking` only when the model emits it | `message` (last chunk), `run` | `_transcribe_event` and helpers; `test_129_claude_code.py` |
| `codex` | `system`, `user`, `tool_call` and `tool_result` (commandExecution, fileChange, mcpToolCall, dynamicToolCall, webSearch), `assistant_text`, `thinking`, `usage` at turn end, `result`, `error` | none. Thinking only when streamed | `run` (cumulative) | `_transcribe_*` from `_handle_notification`; item shapes from the generated schema of codex-cli 0.162.1, not a live run |
| `pi` | `system`, `user`, `assistant_text`, `thinking`, `usage`, `tool_call` and `tool_result` (paired on `toolCallId`), `result`, `error` | none. Partial tool output not recorded | `message` | `_transcribe_event` and helpers; event shapes from the published docs; pi not installed here |
| `opencode` | `system`, `user`, `assistant_text`, `tool_call`, `tool_result`, `error`, `result` | `thinking`, `usage` | n/a | `OpenCodeTranscriptTap` (best-effort methods); `test_129_opencode.py` |
| `opencode_compat` | as `opencode`, through the same tap; `result.output` is null | `thinking`, `usage` | n/a | `test_129_opencode_compat.py` |
| `driver` | `capability`, `user`, `tool_call` (call_id null), `thinking`, `usage` (cost events and result line), `error`, `system`, `assistant_text`, `result` | `tool_result` (the contract has no output field) | cost events: absent. result line: `run` | `_transcribe`, `_emit_usage`; `test_129_driver.py` |
| `prime_agent` | `system`, `user`, `assistant_text`, `tool_call`, `tool_result`, `error`, `result` | `thinking`, `usage` | n/a | `_transcribe`; `test_129_prime_agent.py`; field names from the shared Pi/Prime vocabulary |
| `hermes` | `capability`, `system` (launch configuration), `user`, `assistant_text` (raw stdout), `result`, `error` | `tool_call`, `tool_result`, `thinking`, `usage` | n/a | `_record_*`, `_fail_start`, `_fail_spawn`; `test_129_hermes.py` |
| `openclaw` | `capability`, `system`, `user`, `assistant_text` (per payload), `result`, `error` | `tool_call`, `tool_result`, `thinking`, `usage` | n/a | `_record_success`, `_record_failure`; `test_129_openclaw.py` |
| `junie` | `capability`, `system`, `user`, `usage` (result file counts only), `assistant_text`, `result`, `error` | `tool_call`, `tool_result`, `thinking` | `run` | `_record_launch`, `_record_outcome`; `test_129_junie.py` |

Every backend emits exactly one `capability` record per run, except where noted in the code. Claude Code declares nothing unavailable.

## Verification

Run in this worktree. Results are in the final report.

- Daemon writer, dedupe, restart, torn line, seq validation, size bounds, redaction on the bytes, symlink refusal, failure containment, root placement, disabled mode, retention, collector, HTTP delivery (incremental, re-delivery, drain, teardown bound, wall-clock drain bound, disabled, older performer), and stdio payload: `tests/unit/test_129_run_transcript.py`, `tests/unit/test_129_redaction_parity.py`.
- Performer buffer, redaction and secret provider, best-effort capture, batch bound, scope: `agent/performer/tests/unit/test_129_transcript.py` and `test_129_transcript_guards.py`.
- Performer job route, cursor paging, the 500 bound, disabled job: `agent/performer/tests/unit/test_129_transcript_route.py`.
- Per-backend mappings: `agent/performer/tests/unit/test_129_<backend>.py`, and Claude Code's message-usage and `stream_event` cases in `test_129_claude_code.py`.

Tests that fail without their fix: each review fix has a test written against the behaviour it changes. The writer's constructor signature and the root default changed, so the pre-fix code cannot run these tests unmodified. The claim is established by reading each test against the code it replaced, not by reverting the code and running the suite.

## Known gaps

- **Loss window.** Records pulled at the last poll are safe. A pod deleted by the platform without warning loses up to one poll interval (spec).
- **Subprocess transport.** The development-only subprocess transport (`AgentService`) writes no transcript.
- **Unattributed runs.** A poll with no target writes to `unattributed/unattributed` (D12).
- **Cap accounting.** The performer and the daemon count bytes with different serializers, so the cap is accurate to about one record.
- **Dropped counts.** The on-disk marker does not carry the final dropped count, which is logged at release.
- **Live verification.** No backend binary was run against a live model here. Claude Code's mapping is covered by fixtures built from the stream-json shapes the handler reads.
- **Daemon GitHub App token.** Not held in the daemon environment, so only dispatch-time and refreshed values are redacted exactly (D4).
- **Pre-existing code observed, not changed.** `driver.py` `relay_feedback()` calls `self._cancel_reader()` without `await`. The codex `turn/completed` fallback looks for `type == "message"` items, which the v2 schema does not define.
