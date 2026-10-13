# Tasks: Persist a full transcript for every performer run

Checked items are done and verified by running. Unchecked items are open, and each carries its reason.

## Setup and contracts

- [x] T001 Branch `129-run-transcripts` from `origin/main`
- [x] T002 Performer record schema and buffer: `agent/performer/src/performer/transcript.py` (`TranscriptRecord`, `TranscriptUsage`, `TranscriptBuffer`, `emit`, `set_active_buffer`)
- [x] T003 Daemon record schema, writer, collector, retention: `src/coordinare/services/run_transcript.py`
- [x] T004 Wire fields on both sides: `JobInitPayload.transcript_max_bytes`, `JobStatus.transcript` and `JobStatus.transcript_last_seq` (`server/models.py`, `models/performer_endpoint.py`)
- [x] T005 Config: `TranscriptsConfig` (`enabled`, `retention_days`, `max_bytes_per_run`) on `ProjectConfiguration`, `extra="forbid"`
- [x] T006 Commented `transcripts:` block in `config.example.yaml`

## Performer side

- [x] T010 `JobRunner`: buffer created at submit, installed in the job task context, served from `get()` with a clamped limit
- [x] T011 `GET /jobs/{id}` accepts `transcript_after` (ge 0) and `transcript_limit` (1 to 500)
- [x] T012 Claude Code: full mapping of every stream-json event type (`_transcribe_event` and helpers), plus `capability` and `user` at start and relay
- [x] T013 codex mapping (JSON-RPC item and turn notifications)
- [x] T014 pi mapping (JSON event stream)
- [x] T015 opencode and opencode_compat mapping (shared tap)
- [x] T016 driver and prime_agent mapping
- [x] T017 hermes, openclaw and junie mapping, with start-path failure records
- [x] T018 Performer tests: `test_129_transcript.py`, `test_129_transcript_route.py`, `test_129_claude_code.py`, `test_129_codex.py`, `test_129_pi.py`, `test_129_opencode.py`, `test_129_opencode_compat.py`, `test_129_driver.py`, `test_129_prime_agent.py`, `test_129_hermes.py`, `test_129_openclaw.py`, `test_129_junie.py`

## Daemon side

- [x] T020 `HTTPPerformerService`: run id is the session id (payload `job_id`), cap sent with the payload, writer resolved per run, cursor on every status poll
- [x] T021 Terminal drain before teardown, bounded, with stalled and failed logs
- [x] T022 `HTTPPerformerService.check_status` passes cursor only when transcripts are on (disabled runs send nothing extra)
- [x] T023 `PerformerHTTPClient.get_job` keyword arguments for the cursor
- [x] T024 Monitor passes `transcript_target` (symphony, card) in `phase_458` and the observer stall probe
- [x] T025 `__main__`: collector built from config and the attempt-log base, passed to services and into the coordinare state
- [x] T026 Daemon: retention sweep at start and every 24 hours, off the cycle loop (`asyncio.to_thread`), task cancelled on stop
- [x] T027 Daemon tests: `tests/unit/test_129_run_transcript.py` (writer, redaction, cap, restart, torn line, retention, delivery)

## Documentation

- [x] T030 `specs/129-run-transcripts/spec.md`
- [x] T031 `specs/129-run-transcripts/plan.md` (decisions, delivery protocol, redaction placement, default directory, capability table)
- [x] T032 `AGENTS.md` Recent Changes entry at the top

## Verification

- [x] T040 `ruff check src tests agent/performer/src` clean
- [x] T041 `mypy -p coordinare` clean (with `MYPYPATH` pointing at the worktree)
- [x] T042 `scripts/ruff_baseline.py` holds (exit 0)
- [x] T043 No suppression markers in added lines (`type: ignore`, `noqa`, skip, xfail)
- [x] T044 Whole daemon tree and whole performer suite (counts in the final report)
- [ ] T045 A live Claude Code run with a real model, checking a transcript end to end. Not done: no model access in this environment
- [ ] T046 Live runs for the non-Claude backends. Not done: codex, pi, opencode, prime_agent, driver, hermes, openclaw and junie binaries were not run. Their mappings are from code and constructed events
- [ ] T047 A kill-the-pod test in a real cluster. Not done. The delivery test covers the sequence the daemon sees, not a real pod deletion

## Open items and known gaps

- [ ] T050 Subprocess transport (`agent_transport: subprocess`) writes no transcript. It does not use `HTTPPerformerService`. Out of scope for the performer-run path.
- [ ] T051 `performer_log_dir` is mounted read-write into ephemeral containers, so the transcripts folder is visible to the agent inside the container. Pre-existing for the attempt logs too. Follow-up: move transcripts outside the mount.
- [ ] T052 Claude Code `start()` and `relay_feedback()` emit `capability` and `user` records that no unit test covers. The stream-json mapping is covered.
- [ ] T053 A status poll with no transcript target writes to `unattributed/unattributed/`. Complete, but filed under the wrong folder.
- [ ] T054 The on-disk `truncated` marker does not carry the final dropped count. The count is logged at release.
