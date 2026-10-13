# Tasks: Persist a full transcript for every performer run

Checked items are done and verified by running. Unchecked items are open, and each carries its reason.

## Original build

- [x] T001 Branch `129-run-transcripts` from `origin/main`
- [x] T002 Performer record schema and buffer: `agent/performer/src/performer/transcript.py`
- [x] T003 Daemon record schema, writer, collector, retention: `src/coordinare/services/run_transcript.py`
- [x] T004 Wire fields on both sides: `JobInitPayload.transcript_max_bytes`, `JobStatus.transcript` and `JobStatus.transcript_last_seq`
- [x] T005 Config block `transcripts:` on `ProjectConfiguration`
- [x] T006 Commented `transcripts:` block in `config.example.yaml`
- [x] T010 `JobRunner`: buffer per job, installed in the job task context, served from `get()`
- [x] T011 `GET /jobs/{id}` with `transcript_after` and `transcript_limit` (1 to 500)
- [x] T012 Claude Code full mapping; `capability` and `user` at start and relay
- [x] T013 to T017 Mappings for codex, pi, opencode, opencode_compat, driver, prime_agent, hermes, openclaw, junie
- [x] T020 `HTTPPerformerService` run id, cap in payload, writer per run, cursor on every poll
- [x] T024 Monitor passes `transcript_target` in `phase_458` and the observer probe
- [x] T025 `__main__` builds the collector and passes it to the services and the state
- [x] T026 Retention sweep at start and daily, off the cycle loop, cancelled on stop

## Adversarial review revisions

- [x] R001 HIGH 1 root moved to `<state directory>/transcripts`; `transcripts.dir` override; `make_collector` refuses a root inside or containing a performer mount and disables transcription with an error log (`test_root_inside_a_performer_mount_is_refused`, `test_mount_inside_the_root_is_refused_too`)
- [x] R002 HIGH 1 Kubernetes verified by reading `kubernetes_runtime.py` (only devenv PVC subpaths are mounted). Recorded in plan D5
- [x] R003 HIGH 2 `transcript_path` is lexical; writer refuses symlinks at every component and at the file; `O_NOFOLLOW` (`test_symlinked_card_directory_is_refused_and_nothing_is_written`, `test_planted_run_file_symlink_is_refused_without_writing_the_target`, `test_symlink_pointing_outside_the_root_does_not_raise`)
- [x] R004 HIGH 3 writer never raises; `append_batch`, constructor and `writer_for` contain failures; the service's absorb, drain, writer and teardown helpers are contained; the terminal cleanup always runs (`test_root_that_is_a_file_fails_the_run_without_raising`, `test_writer_exception_never_escapes_the_status_poll`, performer `test_emit_never_raises_when_capture_breaks`)
- [x] R005 HIGH 3 terminal drain bounded by wall clock (`asyncio.timeout`, 30 s) (`test_terminal_drain_has_a_wall_clock_bound`)
- [x] R006 HIGH 4 shared redaction module: credential-named keys (substring, case-insensitive), `NAME=value` and JSON-quoted forms, embedded JSON, token shapes (`gh*_`, `github_pat_`, `sk-ant-`, `sk-litellm-`, `sk-proj-`, `sk-`, `AKIA`, Bearer, Authorization, Slack) (`test_every_secret_shape_is_absent_from_the_bytes_on_disk`, performer `test_shared_redaction_handles_the_review_shapes`)
- [x] R007 HIGH 4(d) exact values: performer from the job payload and refreshed secrets (`test_job_secrets_and_credential_keys_never_reach_the_stored_record`, `test_secret_provider_is_read_per_record`); daemon from dispatch secrets, refreshes and ambient credentials (`test_daemon_collector_redacts_ambient_credentials`)
- [x] R008 HIGH 4 the two copies are byte-identical, enforced (`test_daemon_and_performer_redaction_modules_are_byte_identical`)
- [x] R009 MEDIUM 5 seq validation: window, `last_seq`, strictly increasing (`test_poisoned_seq_does_not_drop_later_records`, `test_seq_above_the_reported_last_seq_is_rejected`, `test_non_increasing_seq_within_a_batch_is_rejected`)
- [x] R010 MEDIUM 5 size bounds: per-record marker, per-batch cut with progress (`test_oversize_record_becomes_a_truncation_marker`, `test_batch_byte_limit_defers_the_rest_to_the_next_poll`, performer `test_batches_are_byte_bounded_and_always_progress`)
- [x] R011 MEDIUM 6 final drain before `release_session`, `stop_session_confirmed` and the transport-error cleanup, each bounded at 5 s (`test_release_session_drains_before_teardown`, `test_hanging_performer_cannot_delay_teardown_beyond_its_bound`)
- [x] R012 MEDIUM 6 loss window stated in spec and plan
- [x] R013 MEDIUM 7 usage kept per message as the last chunk, written at message end, `scope` on every usage record (`test_full_run_is_recorded_in_order` now asserts output 40 and scope `message`; `test_usage_scope_is_kept_on_disk`)
- [x] R014 LOW 8 disabled touches nothing, including retention (`test_disabled_touches_nothing_on_disk`; retention in `_start_transcript_retention` checks `enabled`)
- [x] R015 LOW 9 bounds on `max_bytes_per_run` (1 KiB to 1 GiB) and `retention_days` (0 to 3650) (`test_config_defaults_bounds_and_yaml_dir_alias`)
- [x] R016 LOW 11 unknown and `stream_event` records kept as `system` (`test_stream_event_and_unknown_types_are_kept_not_dropped`)
- [x] R017 LOW 12 covered by R006 (the reviewer listed it as part of 4(c))
- [x] R018 LOW 13 stdio receiver keeps the `transcript_target` key (`test_stdio_status_carries_the_transcript_target_to_the_performer`)
- [x] R019 Specs, plan and tasks updated for location, redaction scope, best-effort guarantee, loss window and seq validation
- [x] R020 Lint, mypy, ratchet, quality guard

## Open items

- [ ] O001 Fails-without-fix was not verified by reverting each fix and running its test. The old writer signature is gone, so the old code cannot run these tests unchanged. Each test is written against the behaviour it changes.
- [ ] O002 Live runs for every backend. No model access in this environment.
- [ ] O003 A pod killed by the platform between polls loses up to one poll interval (spec, known loss window).
- [ ] O004 Subprocess transport writes no transcript (out of scope for performer runs).
- [ ] O005 A poll with no transcript target writes to `unattributed/unattributed/`.
- [ ] O006 The on-disk `truncated` marker does not carry the final dropped count; it is logged at release.
- [ ] O007 The daemon's minted GitHub App token is not held in its environment, so it is redacted only where the dispatch or a refresh supplied it.
- [ ] O008 Claude's last-chunk usage rule rests on reading the handler and fixtures, not a captured stream in which a later chunk raised the count.
