# Plan: Persist a full transcript for every performer run

Implements `spec.md`. This file records the design and the decisions behind it.

## Architecture

Two packages, one contract. The performer runs in a container or pod that is deleted after the run. The daemon owns the file.

```
backend (claude_code, codex, ...)
   |  transcript.emit(type, text=..., input_data=..., ...)      performer/transcript.py
   v
TranscriptBuffer (per job, in the job runner)                      seq-numbered, byte-capped, redacted
   ^
   |  GET /jobs/{id}?transcript_after=N&transcript_limit=L
   |  -> JobStatus.transcript (records seq > N), JobStatus.transcript_last_seq
   |
HTTPPerformerService.check_status (daemon)                          coordinare/services/http_performer_service.py
   |  RunTranscriptWriter.append_batch (dedupe by seq, cap, redact)  coordinare/services/run_transcript.py
   v
<performer_log_dir>/transcripts/<symphony>/<card>/<run_id>.jsonl
```

The performer never imports coordinare, and coordinare never imports the performer. The two record models mirror each other by field name. This is the same arrangement the wire protocol already uses (`performer/protocol.py` mirrored in `coordinare/protocol.py`).

## Decisions

### D1. Full-fidelity channel separate from `BackendEvent`

`BackendEvent.text` is capped at 200 characters and `drain_events()` feeds the activity log, which is bounded on purpose. Transcripts must not reuse it. Each backend emits transcript records at its raw parse site (the decoded JSON, the stream delta, the stdout buffer) and leaves its existing `_emit` calls alone. The existing activity log behaves exactly as before.

### D2. Run identity is the existing coordinare session id

`_dispatch_card_locked` already allocates `session_id = uuid4()` before the container starts, and uses it as the Docker label and the `_active_jobs` key. The job payload previously received a second, unrelated uuid as `job_id`. The daemon now copies the session id into the payload's `job_id` (`payload.model_copy(update={"job_id": session_id, ...})`). In persistent mode the two were already equal. So:

- the performer's record `run_id` is the session id,
- the file name is the session id,
- no second identifier is introduced.

The daemon also overwrites `run_id` on write, so the file is self-consistent even if a future performer changes its job id.

### D3. Delivery: pull by cursor, drain before teardown

Rejected alternative: have the performer push records to the daemon. The daemon already polls, and a push would need a new inbound endpoint and retry logic.

Chosen protocol (pull, piggybacked on the existing status poll):

- Request: `GET /jobs/{id}?transcript_after=N&transcript_limit=L`. The daemon sends the query only when transcripts are enabled, so disabled runs add nothing to the wire.
- Response (additive fields on `JobStatus`): `transcript` (list of records with `seq > N`, ascending) and `transcript_last_seq` (the performer's highest seq, `null` when the job has no buffer).
- Batch bound: the daemon asks for 200 per ordinary poll and 500 per drain request. The route clamps `transcript_limit` to 1 through 500 (FastAPI `Query(ge=1, le=500)` rejects larger values with 422). A single status response is therefore bounded to 500 records.
- Cursor: the daemon's cursor for a run is the highest seq in its file. It is recovered from the file when the writer is created, so a restarted daemon resumes without a separate store and without a schema change.
- Dedupe: `append_batch` skips any record whose seq is at or below the cursor. A re-delivered batch writes nothing.
- Terminal drain: the ephemeral container is torn down right after a terminal status poll, and the performer's buffer dies with it. So on a terminal poll the daemon loops `GET ... transcript_after=cursor` until `cursor >= transcript_last_seq`, before the cleanup and before the persistent-mode reset. The loop is bounded (5000 rounds) and logs `run_transcript.drain_stalled` or `run_transcript.drain_failed` instead of looping forever.

Failure modes, stated plainly: records the daemon never pulled are lost if the pod dies before the next poll. Polls happen every cycle, so the window is one poll interval. The file itself is never lost once written.

### D4. Redaction runs twice, at the producer and at the writer

- Performer (`TranscriptBuffer.append`): `_redact_secrets` (the performer's token patterns) on every string, and masking of sensitive keys (`token`, `password`, `secret`, `api_key`, `authorization`, ...) in `input` and `output`. Records are redacted before they are stored, so they are never held unredacted in memory.
- Daemon (`RunTranscriptWriter._prepare` and `_to_line`): `redact_value` (the shared `coordinare.lib.redaction` key and value rules) on the record structure, then `redact_secrets` on the serialized line. The line-level pass catches anything the structure pass cannot see, such as a secret inside a JSON key.

Two passes are cheap and protect against a performer image that predates this change or a backend that forgets to call the helper.

### D5. Where files go

- With `performer_log_dir` set: `<performer_log_dir>/transcripts/`. The daemon already mounts `performer_log_dir` into ephemeral containers at `/var/log/performer`, so the layout sits next to the attempt logs.
- With it unset: `<config directory>/logs/transcripts`, the same base the attempt log uses (`_resolve_attempt_log_dir`). If that path is not writable (a read-only ConfigMap, issue 493), the root falls back to `<state directory>/logs/transcripts`.

Path safety: `symphony`, `card_id` and `run_id` pass through `sanitize_segment`, which keeps `[A-Za-z0-9._-]`, replaces everything else with `_`, strips leading and trailing dots and underscores, caps length at 120, and maps an empty result to `unattributed`. `transcript_path` then verifies the resolved path is inside the resolved root and raises `ValueError` otherwise.

### D6. Symphony and card attribution

The monitor knows the symphony (`state["current_symphony"]`) and the card id. It passes `transcript_target: {symphony, card_id}` in the status payload on every poll: in `phase_458` (the normal poll) and in the observer stall probe. The service remembers the target per run. A poll that carries no target (for example a bootstrap poll from `daemon.py`) writes to `unattributed/unattributed/<run_id>.jsonl` rather than losing records. Such a file is complete but filed under the wrong folder. This is a known, deliberate gap.

### D7. Size cap

The cap is enforced in two places, with the same rule:

- the performer buffer (`max_bytes` is sent in the job payload as `transcript_max_bytes`),
- the daemon writer (`max_bytes_per_run`).

When the next content record would exceed the cap, the record is replaced by one `truncated` marker (its seq is the dropped record's seq, so the cursor logic is unchanged) and every later content record is counted, not written. A `truncated` record already in the file is never repeated, and the daemon detects that state after a restart by scanning the file.

Dropped counts (`dropped_records`, `dropped_bytes`) are kept in memory and logged once in `run_transcript.run_truncated` when the writer is released. They are not written into the marker, because the marker is written at the moment the cap is hit, before the drop count is known. The on-disk marker therefore reports `max_bytes` and `written_bytes` only.

The performer buffer is memory-bounded by the same cap, so a run cannot grow the job runner without limit.

### D8. Retention

- `retention_days: 0` keeps forever. The default is 30 days.
- A sweep runs once at daemon start and then every 24 hours (`_transcript_retention_loop`). It runs `prune_expired` through `asyncio.to_thread`, so the cycle loop is never blocked. The task is stored on the daemon and cancelled in `stop()`.
- `prune_expired` walks the root with `os.walk(followlinks=False)`, removes only regular files named `*.jsonl` whose resolved path is inside the resolved root, skips symlinks entirely, and then removes folders left empty (never the root itself).
- Age is the file's mtime. An active run's file is appended to, so its mtime stays current and it is not pruned mid-run.
- Retention still runs when `enabled: false`, so old files are cleaned up after an operator turns collection off.

### D9. Disabled mode

`transcripts.enabled: false`:

- the job payload carries `transcript_max_bytes: null`, so the performer creates no buffer and `emit` is a no-op (`transcript_enabled()` is false),
- the status request carries no `transcript_after` parameter (`get_job` omits it),
- no writer is created and no file is written.

### D10. Schema shape

One record type for all backends. The issue's fields are kept (`ts`, `run_id`, `seq`, `type`, `tool_name`, `input`, `output`, `is_error`, `usage`). Two are added:

- `text`: the prose body (assistant text, thinking, user prompt, error message, capability reason, result summary). Keeping prose out of `output` makes the file readable with `jq`.
- `call_id`: correlates a `tool_call` with its `tool_result`. Needed because parallel tool calls can complete out of order.

`usage` is an object with `input_tokens`, `output_tokens`, `cache_read`, `cache_write`, `model`. Unknown fields are `null`. A backend never fabricates a count.

### D11. Where the configuration lives

`TranscriptsConfig` is a top-level block (`transcripts:`) on `ProjectConfiguration`, next to `performer_log_dir`, with `extra="forbid"` as the newer blocks do. It is not per symphony, because the file layout already separates symphonies.

## Wire and contract changes

| Surface | Change | Compatibility |
|---|---|---|
| Performer `JobInitPayload` (`server/models.py`) | `transcript_max_bytes: int \| None = None` | older performers ignore unknown fields (`extra="ignore"`) |
| Coordinare `JobInitPayload` (`models/performer_endpoint.py`) | same field, `exclude_none` on the wire | `None` is omitted |
| Performer `JobStatus` (`server/models.py`) | `transcript: list`, `transcript_last_seq: int \| None` | defaults empty |
| Coordinare `JobStatus` (`models/performer_endpoint.py`) | same two fields | defaults empty |
| `GET /jobs/{id}` | optional `transcript_after` (ge 0), `transcript_limit` (1 to 500) | absent params keep today's response |
| Coordinare `PerformerHTTPClient.get_job` | optional keyword arguments | default call unchanged |

The `protocol.py` mirrors (`ProtocolResponse`, `ProtocolMessage`) are not changed. Transcript data travels on the job status, not on the protocol response.

## Per-backend capability table

"Provided" means the backend's own output carries the information. "Unavailable" is what goes into the `capability` record. Each row was checked by reading the parse code named, and by a unit test that drives that code with captured or constructed events (see Verification).

| Backend (file) | Records emitted, and from where | Declared unavailable (in `capability`) | How it was established |
|---|---|---|---|
| `claude_code` (`backends/claude_code.py`) | `system` (init and other subtypes, full event), `user` (prompt; relayed feedback), `assistant_text`, `thinking`, `tool_call` (full input), `tool_result` (full content, `is_error`, correlated by `tool_use_id`), `usage` (per assistant message, deduplicated by message id; run total from `result`), `result`, `error` | none. `thinking` appears only when the model emits thinking blocks | `_transcribe_event` and helpers, fed from `_handle_event` for every parsed stream-json event. Tested in `test_129_claude_code.py` |
| `codex` (`backends/codex.py`) | `system` (thread start response), `user` (`turn/start` input; relay), `tool_call` and `tool_result` from `commandExecution`, `fileChange`, `mcpToolCall`, `dynamicToolCall`, `webSearch` items (correlated on item id), `assistant_text` (completed `agentMessage` item), `thinking` (reasoning item, assembled from `item/reasoning/textDelta`), `usage` (cumulative token totals at turn end), `result`, `error` | none. Thinking appears only when the model streams reasoning. Usage is cumulative per thread, so a relayed turn adds a later cumulative record and the last one is the run total | `_transcribe_notification`, `_transcribe_item_completed`, `_transcribe_turn_end`, from `_handle_notification`. Item shapes come from the generated app-server schema in the installed codex CLI (0.162.1), not from a live run |
| `pi` (`backends/pi.py`) | `system` (session, agent start), `user` (prompt; relay), `assistant_text`, `thinking`, `usage` (provider dependent; skipped when all four counts are missing), `tool_call` and `tool_result` (paired on `toolCallId`), `result` (agent end), `error` | none. Partial `tool_execution_update` output is not recorded, and this is stated in the capability text | `_transcribe_event`, `_transcribe_assistant_message`, `_transcribe_agent_end`, from the stdout reader. Event shapes follow the published `pi-coding-agent` JSON mode docs. pi is not installed in this environment, so no live run |
| `opencode` (`backends/opencode.py`) | `system` (session and model), `user`, `assistant_text` (flushed before tool records and at turn end, from cumulative text parts), `tool_call` and `tool_result` (tool parts), `error`, `result` | `thinking`, `usage` (no reasoning or token fields are read by this adapter) | `OpenCodeTranscriptTap.observe` fed from `_handle_event`, reading the same parts the adapter already reads. Tested in `test_129_opencode.py` |
| `opencode_compat` (`backends/opencode_compat.py`) | same as `opencode`, through the same tap | `thinking`, `usage` | same tap, imported from `opencode.py`. Tested in `test_129_opencode_compat.py`. Its `result.output` is `null`, because this adapter does not keep assembled output (mirrors existing behaviour) |
| `driver` (`backends/driver.py`) | `capability`, `user`, `tool_call` (one per activity event, `call_id` null), `thinking`, `usage` (from cost events), `error`, `system` (non-delta progress and unknown event types), `assistant_text` (delta progress flushed on the next non-delta line), `result` | `tool_result`. The driver contract defines one `tool_use` event per activity with no output field | `_transcribe` from `_handle_line`, plus `_record_outcome`. Tested in `test_129_driver.py`. Unconfirmed: that driver sets `is_delta` on progress lines in practice |
| `prime_agent` (`backends/prime_agent.py`) | `system` (session header, agent start), `user`, `assistant_text` (from `text_delta`, flushed at message end, tool start, agent end and EOF), `tool_call` and `tool_result` (`tool_execution_start` and `_end`), `error`, `result` (once at EOF) | `thinking`, `usage`. The parser reads only `text_delta` from message updates | `_transcribe` from `_read_loop`. Tested in `test_129_prime_agent.py`. Event field names come from the shared Pi/Prime vocabulary in the module docstring, not from a captured fixture |
| `hermes` (`backends/hermes.py`) | `capability`, `system` (launch configuration, once per launch), `user` (full briefing, plus the `-q` pointer when offloaded), `assistant_text` (raw stdout after exit), `result` (rc, output), `error` (missing env, spawn failure, non-zero exit, reader exception) | `tool_call`, `tool_result`, `thinking`, `usage`. The adapter reads stdout only after exit, `--quiet` hides tool previews, and the `tokens` key it parses is model-written JSON rather than a usage count | `_record_capability_once`, `_record_launch`, `_record_outcome`, `_fail_start`, `_fail_spawn`, from the existing launch and reader code. Tested in `test_129_hermes.py` |
| `openclaw` (`backends/openclaw.py`) | `capability`, `system`, `user`, `assistant_text` (one per non-empty `payloads` entry, or the `meta` final text), `result`, `error` (non-zero exit, malformed output, unclean stop, reader exception) | `tool_call`, `tool_result`, `thinking`, `usage`. Output is one JSON object printed at exit | `_record_success`, `_record_failure`, `_record_capability_once`, from the one-shot stdout parse. Tested in `test_129_openclaw.py` |
| `junie` (`backends/junie.py`) | `capability`, `system` and `user` (per launch, so relay launches record their feedback), `usage` (only when the JSON result file holds integer token fields), `assistant_text` and `result` (from the result outcome), `error` (non-zero exit, spawn failure, reader exception) | `tool_call`, `tool_result`, `thinking` | `_record_launch`, `_record_outcome`, `_record_capability_once`. The JSON result key names are copied from the existing parser, whose comment says the schema is not public. Tested in `test_129_junie.py` |

Every backend emits exactly one `capability` record per run. Claude Code emits it at `start()`, and the other backends emit it from their first launch or failure path. Claude Code's record lists nothing as unavailable and says so.

## Verification

Run in this worktree (`/Users/jasonberry/Workspaces/ViviDynamics/coordinare/.claude/worktrees/agent-af27aeee616922a59`, branch `129-run-transcripts`). The results are in the final report.

- Daemon writer, dedupe, restart, torn line, redaction, byte cap, path safety, retention, collector, HTTP delivery (incremental, re-delivery, drain, disabled, older performer): `tests/unit/test_129_run_transcript.py`.
- Performer buffer, redaction, byte cap, runner delivery: `agent/performer/tests/unit/test_129_transcript.py`.
- Performer job route, cursor paging, the 500 bound, disabled job: `agent/performer/tests/unit/test_129_transcript_route.py`.
- Per-backend mappings: `agent/performer/tests/unit/test_129_<backend>.py`.

## Known gaps

- **Subprocess transport.** The development-only subprocess transport (`AgentService`, `agent_transport: subprocess`) does not go through `HTTPPerformerService` and writes no transcript. The issue is about performer runs, so this is out of scope for this change.
- **Container-visible transcripts.** `performer_log_dir` is mounted read-write into ephemeral containers at `/var/log/performer`. The `transcripts/` folder sits under that mount, so an agent running in a container can read, and in principle alter, other runs' transcript files. The attempt logs already have the same exposure. This is pre-existing in kind. It deserves a follow-up that moves transcripts outside the mount.
- **Relaunch and the start path in Claude Code.** The `capability` and `user` records emitted from `ClaudeCodeBackend.start()` and `relay_feedback()` are not unit-tested. The mapping of stream-json events is.
- **Live verification.** No backend binary was run against a live model in this environment. The mappings for codex, pi, opencode, prime_agent, driver, hermes, openclaw and junie were derived from code and captured or constructed events. Field names the code does not confirm are listed in the per-backend notes above.
- **Unattributed runs.** A status poll without a transcript target writes to the `unattributed` folder (D6).
- **Cap accounting.** The performer and the daemon count bytes slightly differently (different serializers). The cap is approximate to within a record.
- **Dropped counts.** The on-disk marker does not carry the final dropped count (D7).
- **Pre-existing code observed, not changed.** `driver.py` `relay_feedback()` calls `self._cancel_reader()` without `await`. The codex `turn/completed` fallback looks for `type == "message"` items, which the v2 schema does not define. Both are outside this issue.
