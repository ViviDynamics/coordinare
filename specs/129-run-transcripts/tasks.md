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

## Second adversarial review

- [x] S001 Item 1, quadratic redaction: linear word tokeniser (`_redact_assignments`). `test_one_megabyte_unbroken_alphanumeric_run_redacts_in_well_under_a_second`, `test_long_runs_containing_key_stems_are_linear`
- [x] S002 Item 2, I/O on the loop: writer lazy, tail-only cursor read, writes and loads on worker threads, performer redaction at serve time on a thread. `test_cursor_is_read_from_the_tail_not_the_whole_file`, `test_batch_writes_run_on_a_worker_thread_not_the_event_loop`, performer `test_transcript_batches_are_served_on_a_worker_thread`
- [x] S003 Item 3, retention and validation: exact layout, live-file skip, `~` expansion, equal-or-contains protected dirs, env-cache and mount checks, case-insensitive compare, dispatch-time mount conflict. `test_retention_only_removes_the_writers_layout_and_live_files`, `test_expanduser_applies_to_the_transcripts_dir`, `test_root_equal_to_or_containing_a_protected_directory_is_refused`, `test_root_inside_an_env_cache_root_is_refused`, `test_case_insensitive_filesystem_matches_names_case_insensitively`, `test_dispatch_with_a_mount_inside_the_root_is_not_transcribed`
- [x] S004 Item 4, hard links: `st_nlink` checked on load and write. `test_hard_link_inside_the_root_is_refused_and_the_outside_file_untouched`, `test_torn_line_is_not_trimmed_through_a_hard_link`
- [x] S005 Item 5, redaction leaks and over-redaction: escaped JSON, URL userinfo, PEM, unquoted `=` to end of line, whole-word stems. `test_escaped_json_inside_a_string_is_redacted`, `test_url_userinfo_password_is_redacted_and_the_host_kept`, `test_pem_private_key_block_is_redacted_whole`, `test_unquoted_equals_value_is_redacted_to_end_of_line`, `test_tokenizer_and_plural_tokens_are_not_treated_as_credentials`, `test_prose_token_colon_redacts_one_word_by_design`
- [x] S006 Item 6, per-run state: released on every teardown path. `test_cleanup_releases_every_per_run_daemon_record`
- [x] S007 Item 7, finished runs and no-id usage: no writer after release, cursor-ahead logged, last chunk only for no-id messages. `test_finished_run_never_gets_a_writer_again`, `test_cursor_ahead_of_the_performer_is_logged`, `test_chunks_without_a_message_id_are_written_once_with_the_last_usage`
- [x] S008 Item 8, disabled payload restored: `test_disabled_transcripts_leave_the_payload_exactly_as_before`, `test_enabled_transcripts_use_the_session_id_as_the_job_id`
- [x] S009 Item 9, deployment note in the spec (no compose change)

## Third adversarial review

- [x] T001 Redaction recursion: depth bound and a text fallback that never returns the unredacted value (`test_deep_brackets_do_not_raise_and_come_back_as_text`, `test_secret_inside_deep_brackets_is_still_redacted`, `test_deep_structure_is_bounded_not_recursed_without_limit`)
- [x] T002 Per-record guards: withheld `error` marker in `after()`, in `append()`, and in the writer; route returns status with `transcript_error` (performer `test_a_failing_batch_still_returns_the_status_with_an_error_flag`, `test_a_record_that_cannot_be_redacted_is_served_as_a_withheld_marker_with_its_seq`, `test_a_record_that_cannot_be_captured_keeps_its_seq_and_the_run_goes_on`; daemon `test_a_record_that_cannot_be_recorded_becomes_a_marker_and_the_run_continues`)
- [x] T003 Lines are JSON: no line-level redaction; property test over nasty strings; cursor scans back (`test_every_line_parses_and_keeps_its_seq_for_nasty_strings`, `test_line_level_pass_no_longer_corrupts_prose_with_a_marker`, `test_cursor_is_recovered_past_an_unparsable_last_line`)
- [x] T004 FIFO does not hang; failed load never writes; short writes completed; failed write truncates (`test_fifo_at_the_run_path_fails_the_run_without_hanging`, `test_a_failed_load_never_reaches_the_write`, `test_short_writes_are_completed`, `test_failed_write_leaves_no_torn_line`)
- [x] T005 Terminal state returned before the drain; per-poll bounds (`test_terminal_status_is_returned_before_the_drain_finishes`, `test_a_slow_write_does_not_hold_up_the_poll`, `test_a_slow_first_cursor_read_falls_back_to_a_plain_poll`)
- [x] T006 Over-redaction of code and counts; PEM scoped to BEGIN..END; BEGIN without END only with base64 (`test_code_and_counts_are_not_redacted`, `test_a_quoted_pem_marker_in_prose_does_not_eat_the_text_after_it`, `test_a_pem_block_is_redacted_only_from_begin_to_end`, `test_a_begin_without_end_redacts_only_when_base64_lines_follow`)
- [x] T007 Redaction gaps: netrc, bare netrc line, JWT, curl -u, ANSI, key variants, Stripe, PGP, descriptive suffixes; 5 MB linearity (`test_netrc_password_is_redacted`, `test_bare_jwt_is_redacted`, `test_curl_user_password_is_redacted`, `test_ansi_sequences_between_key_and_value_do_not_hide_the_secret`, `test_structured_key_variants_are_masked`, `test_stripe_live_key_is_redacted`, `test_pgp_private_key_block_is_redacted`, `test_five_megabyte_unbroken_word_redacts_in_well_under_two_seconds`)
- [x] T008 Retention: racing folder, minimum age, shutdown exit (`test_a_card_folder_racing_a_writer_does_not_abort_the_sweep`, `test_the_sweep_skips_files_within_the_minimum_age`, `test_the_retention_loop_exits_on_shutdown`)
- [x] T009 Directories private; distinct names; out-of-order batch; lone surrogate; secrets released (`test_new_directories_have_no_group_or_other_access`, `test_distinct_ids_never_share_a_file_name`, `test_out_of_order_batch_is_sorted_and_written_whole`, `test_a_lone_surrogate_is_written_not_dropped`)
- [x] T010 Record size bounds on the performer (`test_a_huge_text_field_is_cut_with_a_marker_before_buffering`, `test_a_record_too_large_even_after_field_bounds_has_its_payload_withheld`)
- [x] T011 Refreshed secrets under a lock (`test_refreshed_secrets_reach_the_redaction_set_of_the_running_job`, `test_a_refresh_during_a_secret_read_does_not_raise`)

## Fourth adversarial review

- [x] F001 Linear prefix shapes and JWTs; 1 MB and 5 MB timing tests (`test_eyj_run_of_one_megabyte_is_linear`, `test_prefix_run_of_one_megabyte_is_linear`, `test_five_megabyte_adversarial_runs_stay_under_five_seconds`)
- [x] F002 Linear netrc; 1 MB timing test (`test_machine_repeats_of_one_megabyte_are_linear`)
- [x] F003 Linear PEM: repeated BEGIN on one line, and twenty thousand distinct kinds (`test_begin_repeated_on_one_line_of_one_megabyte_is_linear`, `test_twenty_thousand_distinct_pem_kinds_are_linear`)
- [x] F004 Bounded poll: per-record time budget, size checked before the walk (performer `test_one_slow_record_ends_the_batch_so_completion_is_not_held_up`, `test_a_huge_value_is_withheld_by_its_encoded_size_before_any_walk`, `test_a_batch_of_adversarial_records_is_served_within_the_budget`, `test_an_adversarial_jwt_field_of_one_megabyte_is_served_quickly`)
- [x] F005 Relief leaks closed: each leaking example is a test (`test_relief_rules_do_not_leak`, `test_numeric_structured_values_under_a_credential_key_are_redacted`); round-three cases still hold (`test_round_three_over_redaction_cases_still_hold`, `test_short_numbers_and_descriptive_names_are_kept`)
- [x] F006 Inline terminal drain and teardown; no background finish (`test_terminal_status_is_returned_after_the_drain_finishes`, `test_terminal_drain_completes_before_the_terminal_status_is_returned`)
- [x] F007 Tail that does not fit is marked truncated (`test_a_tail_that_does_not_fit_the_bound_is_marked_truncated`, `test_record_tail_lost_writes_one_truncated_marker_after_the_last_written_record`)
- [x] F008 One drain and one teardown per run (`test_a_run_is_drained_once_on_the_terminal_path_and_not_again_on_release`)
- [x] F009 Unreadable tail fails the run instead of resetting the cursor (`test_more_than_a_thousand_unparsable_lines_fail_the_writer_without_a_write`)
- [x] F010 OSC escapes, RecursionError in the parser, retention stat guard (`test_osc_sequence_does_not_hide_a_secret`, `test_osc_sequence_with_st_terminator_does_not_hide_a_secret`, `test_a_deeply_nested_tail_line_never_raises_out_of_the_parser`, `test_retention_survives_an_entry_whose_stat_fails`)

## Open items

- [ ] O001 Fails-without-fix was not verified by reverting each fix and running its test. The old writer signature is gone, so the old code cannot run these tests unchanged. Each test is written against the behaviour it changes.
- [ ] O002 Live runs for every backend. No model access in this environment.
- [ ] O003 A pod killed by the platform between polls loses up to one poll interval (spec, known loss window).
- [ ] O004 Subprocess transport writes no transcript (out of scope for performer runs).
- [ ] O005 A poll with no transcript target writes to `unattributed/unattributed/`.
- [ ] O006 The on-disk `truncated` marker does not carry the final dropped count; it is logged at release.
- [ ] O007 The daemon's minted GitHub App token is not held in its environment, so it is redacted only where the dispatch or a refresh supplied it.
- [ ] O008 Claude's last-chunk usage rule rests on reading the handler and fixtures, not a captured stream in which a later chunk raised the count.
