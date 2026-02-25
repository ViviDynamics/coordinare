# Tasks: Configuration Management (008)

**Input**: Design documents from `/specs/008-config-management/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/cli-config-validate.md ✅, quickstart.md ✅

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no incomplete-task dependencies)
- **[Story]**: User story this task belongs to (US1–US4 from spec.md)

---

## Phase 1: Setup

**Purpose**: Create new module skeleton files; confirm no new external dependencies needed.

- [ ] T001 Create skeleton files `src/coordinare/config_discovery.py` and `src/coordinare/config_validation.py` with module docstrings, `__all__ = []`, and a `# TODO: implement` comment; verify `python -c "from coordinare.config_discovery import *; from coordinare.config_validation import *"` succeeds with no import errors

**Checkpoint**: New modules importable — no runtime errors from rest of codebase.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core type definitions shared by all four user stories.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T002 Define all core type definitions in `src/coordinare/config_validation.py`: `DeprecationEntry` dataclass (removed_in: str, replacement_path: str, migration_hint: str); `ErrorType` enum (missing, wrong_type, unknown_field, invalid_value); `ConfigFieldError` dataclass (field_path: str, error_type: ErrorType, fix_hint: str, source: str | None); `ConfigDeprecationWarning` dataclass (field_name: str, removed_in: str, replacement_path: str, migration_hint: str); `ConfigValidationResult` dataclass (errors: list[ConfigFieldError], warnings: list[ConfigDeprecationWarning], config_file_path: Path | None, env_var_fields_count: int, passed: bool); add `passed_strict` property returning `passed and not warnings`; update `__all__` to export all types
- [ ] T003 [P] Create stub `tests/unit/test_config_validation.py` with empty test function bodies (one `pass` each) for all planned test cases: `test_missing_required_field_error`, `test_type_mismatch_error`, `test_all_errors_collected_in_single_pass`, `test_valid_config_passes`, `test_unknown_field_raises_error`, `test_unknown_field_suggests_closest_match`, `test_truly_unknown_field_no_suggestion`, `test_empty_string_env_var_treated_as_absent`, `test_env_var_fields_count_computed`, `test_no_file_env_vars_only_passes`, `test_deprecated_field_raises_warning`, `test_all_spec006_fields_in_registry`, `test_deprecated_field_not_treated_as_unknown`, `test_valid_config_no_deprecation_warnings`
- [ ] T004 [P] Create stub `tests/unit/test_config_discovery.py` with empty test function bodies for: `test_explicit_path_used_when_provided`, `test_explicit_path_error_when_not_found`, `test_explicit_path_error_when_directory`, `test_env_var_config_path_used`, `test_env_var_config_path_error_not_found`, `test_env_var_config_path_error_directory`, `test_cwd_config_yaml_found`, `test_home_config_found_when_cwd_absent`, `test_none_returned_when_no_file_found`; also create stub `tests/unit/test_cli_config_validate.py` with empty bodies for: `test_valid_config_exits_zero`, `test_missing_field_exits_one`, `test_unknown_field_exits_one`, `test_all_errors_reported_in_single_pass`, `test_validate_completes_under_500ms`, `test_env_var_overrides_yaml_field`, `test_no_config_file_all_env_vars_exits_zero`, `test_env_var_type_error_reported_identifies_env_var_name`, `test_no_config_file_missing_fields_exits_one_with_searched_paths`, `test_explicit_path_not_found_exits_one`, `test_deprecated_field_exits_zero_without_strict`, `test_deprecated_field_exits_one_with_strict`, `test_strict_output_shows_deprecation_errors_header`

**Checkpoint**: All type definitions importable; all test stubs collectable by pytest (zero failures, all skipped/pass).

---

## Phase 3: User Story 1 — Config Validation Without Starting the Daemon (Priority: P1) 🎯 MVP

**Goal**: `coordinare config validate [--config PATH]` validates the resolved config in a single pass and exits 0/1 with full error details. No daemon startup.

**Independent Test**: `coordinare config validate --config valid.yaml` exits 0 with one-line success summary. `coordinare config validate --config missing-field.yaml` exits 1 and prints the exact missing field name and a fix hint. All errors in a config file with multiple problems are reported in a single run.

- [ ] T005 [US1] Implement `pre_validate_raw(raw: dict) -> tuple[list[ConfigFieldError], list[ConfigDeprecationWarning]]` in `src/coordinare/config_validation.py`: compute `known_fields = frozenset(ProjectConfiguration.model_fields.keys())`; compute `deprecated_keys = frozenset(DEPRECATION_REGISTRY.keys())` (empty dict for now); for each key in raw: if key in deprecated_keys append ConfigDeprecationWarning; elif key not in known_fields: use `difflib.get_close_matches(key, known_fields, n=1, cutoff=0.6)` for suggestion, append ConfigFieldError(field_path=key, error_type=ErrorType.unknown_field, fix_hint=f"Did you mean '{suggestion}'?..." or "Remove or rename this field.", source="file"); return (errors, warnings); update `__all__`
- [ ] T006 [US1] Implement `validate_config(config_path: Path | None, strict: bool = False) -> ConfigValidationResult` in `src/coordinare/config_validation.py`: load raw dict from YAML file if config_path is not None (return failed result if file not found); call pre_validate_raw(raw) to get pre_errors and pre_warnings; attempt `ProjectConfiguration(**raw)` in a try/except pydantic `ValidationError`; on ValidationError iterate `exc.errors()` and map each to ConfigFieldError using loc tuple as dotted field_path, msg as fix_hint, type as ErrorType (missing→missing, type_error→wrong_type, value_error→invalid_value); compute env_var_fields_count=0 (placeholder — updated in T011); assemble ConfigValidationResult(errors=pre_errors+pydantic_errors, warnings=pre_warnings, config_file_path=config_path, env_var_fields_count=0, passed=len(pre_errors+pydantic_errors)==0); update `__all__`
- [ ] T007 [US1] Refactor `_build_arg_parser()` in `src/coordinare/__main__.py` to add `subparsers = parser.add_subparsers(dest="command")`; add `config_parser = subparsers.add_parser("config")`; add `config_subparsers = config_parser.add_subparsers(dest="config_action")`; add `validate_parser = config_subparsers.add_parser("validate")`; add `--config PATH` and `--strict` flags to `validate_parser`; update `main()` to check `if args.command == "config" and args.config_action == "validate": _cmd_config_validate(args); return` before the existing daemon startup path; preserve all existing daemon flags on the top-level parser
- [ ] T008 [P] [US1] Implement `_cmd_config_validate(args) -> None` in `src/coordinare/__main__.py`: call `validate_config(getattr(args, 'config', None), strict=getattr(args, 'strict', False))`; on success (result.passed): print `✓ Config valid — loaded from {result.config_file_path or "no file (env vars only)"}` and field count summary; if result.warnings: print "DEPRECATION WARNINGS" section with one block per warning (field, removed_in, replacement_path, migration_hint); sys.exit(0); on failure: print `✗ Config validation failed` header, then "ERRORS:" section with one block per ConfigFieldError (field_path, error_type label in brackets, fix_hint), then warnings section if any; if no config file found: print searched paths list; sys.exit(1); follow exact format from contracts/cli-config-validate.md
- [ ] T009 [US1] Fill `tests/unit/test_config_validation.py` with real implementations for US1 tests: `test_missing_required_field_error` (write config yaml missing github_token, call validate_config, assert error with error_type==missing and field_path=="github_token"); `test_type_mismatch_error` (config with poll_interval_seconds="abc", assert error_type==wrong_type); `test_all_errors_collected_in_single_pass` (config missing 3 required fields, assert len(result.errors)==3 in one call); `test_valid_config_passes` (valid config, assert result.passed==True); `test_unknown_field_raises_error` (config with "githubb_token", assert ConfigFieldError with error_type==unknown_field); `test_unknown_field_suggests_closest_match` (assert fix_hint contains "github_token"); `test_truly_unknown_field_no_suggestion` (field "zzzxxx_completely_unknown", assert fix_hint does not contain "Did you mean")
- [ ] T010 [US1] Fill `tests/unit/test_cli_config_validate.py` for US1: `test_valid_config_exits_zero` (write valid config to tmp_path, invoke `_cmd_config_validate` via subprocess or direct call with mocked args, assert sys.exit(0)); `test_missing_field_exits_one` (config missing github_token, assert sys.exit(1) and "github_token" in stdout); `test_unknown_field_exits_one` (config with unknown field, assert sys.exit(1) and "[UNKNOWN_FIELD]" in stdout); `test_all_errors_reported_in_single_pass` (config with 3 distinct errors, assert all 3 appear in stdout without re-running); `test_validate_completes_under_500ms` (time.perf_counter() before/after validate_config call on valid config, assert elapsed < 0.5)

**Checkpoint**: `coordinare config validate --config <path>` fully functional — exits 0/1, reports all errors in one pass, reports field paths + types + fix hints.

---

## Phase 4: User Story 2 — Environment Variable Overrides for CI/CD (Priority: P2)

**Goal**: Any scalar config field can be absent from `config.yaml` and supplied via `COORDINARE_<FIELD>` env var. Env vars always win. Coordinare starts successfully with no config file if all required fields are in env vars.

**Independent Test**: Set `COORDINARE_GITHUB_TOKEN=test123` in environment, omit `github_token` from config.yaml. Run `coordinare config validate`. Verify no missing-field error for github_token. Verify same field set in both file and env var uses env var value.

- [ ] T011 [US2] Update `validate_config()` in `src/coordinare/config_validation.py` to: (1) compute `env_var_fields_count` accurately — after successful model instantiation, iterate scalar fields in `ProjectConfiguration.model_fields`; count fields where `COORDINARE_<FIELD.upper()>` is set in `os.environ` AND either the field was absent from raw dict OR the raw dict value differs from the resolved model value; store count in `ConfigValidationResult.env_var_fields_count`; (2) set `source` on pydantic-caught `ConfigFieldError`s — for each pydantic error in `exc.errors()`, check if the corresponding env var `COORDINARE_<loc[0].upper()>` (or `COORDINARE_<PARENT>__<CHILD>` for nested) is set in `os.environ`; if so, set `source=f"env_var:COORDINARE_{loc[0].upper()}"` on the `ConfigFieldError` (e.g., `COORDINARE_POLL_INTERVAL_SECONDS`); (3) handle `config_path=None` case cleanly — `raw={}`, env vars supply all required fields — this must succeed and produce `passed=True`
- [ ] T012 [P] [US2] Fill `tests/unit/test_config_validation.py` for US2: `test_empty_string_env_var_treated_as_absent` (monkeypatch COORDINARE_GITHUB_TOKEN="", config without github_token, assert missing-field error for github_token); `test_env_var_fields_count_computed` (set 2 COORDINARE_ env vars with values not in config, assert result.env_var_fields_count==2); `test_no_file_env_vars_only_passes` (set all required COORDINARE_ env vars, call validate_config(None), assert result.passed==True)
- [ ] T013 [US2] Fill `tests/unit/test_cli_config_validate.py` for US2: `test_env_var_overrides_yaml_field` (config with github_token="file-value", set COORDINARE_GITHUB_TOKEN="env-value", assert resolved token is env value — no error); `test_no_config_file_all_env_vars_exits_zero` (no config file at any search location, all required COORDINARE_ env vars set, assert exit 0 and "no config file" in stdout); `test_env_var_type_error_reported_identifies_env_var_name` (set COORDINARE_POLL_INTERVAL_SECONDS="abc", assert error output identifies env var name as source alongside field path)

**Checkpoint**: CI/CD pipeline can supply all secrets via `COORDINARE_*` env vars; `coordinare config validate` reports env-var field count in success output.

---

## Phase 5: User Story 3 — Multi-Location Config Discovery (Priority: P3)

**Goal**: `coordinare` and `coordinare config validate` automatically find `config.yaml` in standard locations without requiring `--config` on every invocation.

**Independent Test**: Place a valid `config.yaml` in `~/.coordinare/config.yaml`. Run `coordinare config validate` with no flags. Verify it finds and validates the file without requiring `--config`.

- [ ] T014 [US3] Implement `ConfigDiscoveryError(Exception)` and `discover_config_path(explicit: Path | None) -> Path | None` in `src/coordinare/config_discovery.py` per data-model.md state machine: (1) if explicit provided: if not explicit.exists() raise ConfigDiscoveryError naming path; if explicit.is_dir() raise ConfigDiscoveryError("must point to a file, not a directory"); return explicit.resolve(); (2) check os.environ.get("COORDINARE_CONFIG_PATH"): if set, apply same exist/file checks, return resolved path; (3) cwd_path = Path.cwd() / "config.yaml": if cwd_path.exists(): return cwd_path.resolve(); (4) home_path = Path.home() / ".coordinare" / "config.yaml": if home_path.exists(): return home_path.resolve(); (5) return None; export ConfigDiscoveryError and discover_config_path in `__all__`
- [ ] T015 [P] [US3] Fill `tests/unit/test_config_discovery.py` with all 9 test functions: `test_explicit_path_used_when_provided` (tmp file, assert returned path == file); `test_explicit_path_error_when_not_found` (non-existent path, assert ConfigDiscoveryError); `test_explicit_path_error_when_directory` (tmp_path directory, assert ConfigDiscoveryError with "directory" in message); `test_env_var_config_path_used` (monkeypatch COORDINARE_CONFIG_PATH to tmp file, no explicit, assert file returned); `test_env_var_config_path_error_not_found` (env var set to non-existent, assert ConfigDiscoveryError); `test_env_var_config_path_error_directory` (env var set to directory, assert ConfigDiscoveryError); `test_cwd_config_yaml_found` (monkeypatch Path.cwd() to tmp_path, write config.yaml there, assert returned); `test_home_config_found_when_cwd_absent` (no cwd config.yaml, write ~/.coordinare/config.yaml in tmp, monkeypatch Path.home(), assert returned); `test_none_returned_when_no_file_found` (no file at any location, assert None returned)
- [ ] T016 [US3] Update `validate_config(config_path, strict)` in `src/coordinare/config_validation.py` to call `discover_config_path(config_path)` internally at the start: on ConfigDiscoveryError return a failed ConfigValidationResult with a ConfigFieldError(field_path="config_file", error_type=ErrorType.missing, fix_hint=str(discovery_error)); when discover returns None (no file, no required env vars supplied) include searched paths in fix_hint; store the resolved path in result.config_file_path; remove direct path existence check from previous implementation
- [ ] T017 [US3] Update `main()` daemon startup in `src/coordinare/__main__.py`: replace `ProjectConfiguration.from_yaml(args.config)` with: (1) call `validate_config(args.config)`; (2) on `result.passed==False` configure logging, log each error via `logger.error("config_validation_error", field=e.field_path, error_type=e.error_type.value, hint=e.fix_hint)` for each error in `result.errors`, then `raise SystemExit(2)`; (3) on success **re-instantiate** `ProjectConfiguration` using the resolved path — call `ProjectConfiguration(**_load_raw_yaml(result.config_file_path))` if a file was found, or `ProjectConfiguration()` if env-vars-only (`result.config_file_path is None`); do NOT attempt to extract a config object from `result` (ConfigValidationResult contains no ProjectConfiguration field); (4) log deprecation warnings at `logger.warning` level during daemon startup even without --strict
- [ ] T018 [US3] Fill `tests/unit/test_cli_config_validate.py` for US3: `test_no_config_file_missing_fields_exits_one_with_searched_paths` (no config file at cwd or home, no COORDINARE_ required fields, assert exit 1 and output lists all 4 search locations including "~/.coordinare/config.yaml"); `test_explicit_path_not_found_exits_one` (--config /nonexistent/path.yaml, assert exit 1 and path named in output)

**Checkpoint**: `coordinare config validate` (no flags) works from working directory and home directory. `COORDINARE_CONFIG_PATH` overrides both. `--config` overrides all.

---

## Phase 6: User Story 4 — Deprecation Detection and Migration Guidance (Priority: P4)

**Goal**: `coordinare config validate` detects deprecated config fields and prints precise migration instructions. `--strict` flag escalates warnings to errors for CI pipelines.

**Independent Test**: Config file containing `slack_webhook_url`. Run `coordinare config validate`. Output names the removed field, the spec that removed it (`006-notification-alerting`), and the replacement path (`notifications.channels[].webhook_url`). Config file with no deprecated fields produces no warnings.

- [ ] T019 [US4] Populate `DEPRECATION_REGISTRY: dict[str, DeprecationEntry] = {}` in `src/coordinare/config_validation.py` with all 7 spec-006 entries exactly as specified in data-model.md: slack_webhook_url (removed_in="006-notification-alerting", replacement_path="notifications.channels[].webhook_url", migration_hint as specified); slack_channel; smtp_host; smtp_port; smtp_username; smtp_password; notification_email — each with full removal context and migration instructions
- [ ] T020 [US4] Update `pre_validate_raw()` in `src/coordinare/config_validation.py` to check `DEPRECATION_REGISTRY` before the unknown-field check: for each key in raw dict: if key in DEPRECATION_REGISTRY, create ConfigDeprecationWarning using registry entry and append to warnings list; elif key not in known_fields: append ConfigFieldError(unknown_field) — deprecated fields are NOT also reported as unknown-field errors; ensure the `all_known = known_fields | deprecated_keys` exclusion is applied correctly
- [ ] T021 [US4] Add `--strict` flag to the `validate_parser` in `src/coordinare/__main__.py` (`validate_parser.add_argument("--strict", action="store_true")`); update `_cmd_config_validate` to: in strict mode use "DEPRECATION ERRORS" section header instead of "DEPRECATION WARNINGS"; in strict mode exit 1 when `result.warnings` is non-empty even if `result.errors` is empty; in non-strict mode exit 0 if only warnings present
- [ ] T022 [P] [US4] Fill `tests/unit/test_config_validation.py` for US4: `test_deprecated_field_raises_warning` (config with slack_webhook_url, assert ConfigDeprecationWarning with field_name=="slack_webhook_url" and removed_in=="006-notification-alerting"); `test_all_spec006_fields_in_registry` (assert all 7 field names are keys in DEPRECATION_REGISTRY); `test_deprecated_field_not_treated_as_unknown` (config with slack_webhook_url, assert no ConfigFieldError with error_type==unknown_field); `test_valid_config_no_deprecation_warnings` (clean post-spec-006 config, assert result.warnings==[]); `test_validate_config_reports_all_spec006_deprecated_fields_end_to_end` (write a YAML config containing all 7 deprecated fields as top-level keys, call validate_config(), assert len(result.warnings)==7 and all 7 field names appear in {w.field_name for w in result.warnings} — directly verifies SC-002)
- [ ] T023 [US4] Fill `tests/unit/test_cli_config_validate.py` for US4: `test_deprecated_field_exits_zero_without_strict` (config with slack_webhook_url but otherwise valid, no --strict, assert exit 0 and "DEPRECATION WARNINGS" in stdout and "006-notification-alerting" in stdout); `test_deprecated_field_exits_one_with_strict` (same config with --strict, assert exit 1); `test_strict_output_shows_deprecation_errors_header` (--strict with deprecated field, assert "DEPRECATION ERRORS" in stdout not "DEPRECATION WARNINGS")

**Checkpoint**: All four user stories complete. `coordinare config validate` covers all error types; deprecation warnings report migration path from spec 006; `--strict` enables CI enforcement.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Structured startup log (SC-006), daemon parity, config.example.yaml docs, full suite validation.

- [ ] T024 Add structured startup log entry in `src/coordinare/__main__.py` daemon startup path: after successful `validate_config()` call in `main()`, emit `logger.info("config_loaded", config_file=str(result.config_file_path) if result.config_file_path else "none", env_var_fields_count=result.env_var_fields_count, deprecated_fields_detected=len(result.warnings) > 0)` — log entry must be machine-parseable JSON when `--structured-output` is active (SC-006)
- [ ] T025 [P] Add `test_daemon_startup_emits_config_loaded_log` to `tests/unit/test_config.py`: use structlog testing utilities (or caplog fixture) to capture structured log output during a simulated daemon config load; assert event=="config_loaded", config_file is a non-empty string, env_var_fields_count is an int, deprecated_fields_detected is a bool; run full test_config.py suite and confirm all pre-existing tests still pass unchanged
- [ ] T026 [P] Update `config.example.yaml` to add documentation comments: section header "# Config Discovery" above github_token with note about COORDINARE_CONFIG_PATH env var; add comment block listing 4-path discovery order; add comment noting scalar fields can be overridden with COORDINARE_<FIELD_NAME> env vars; note that list fields (e.g., notifications.channels) must be in the file
- [ ] T027 Run full validation: `cd src && pytest ../tests/unit/test_config_discovery.py ../tests/unit/test_config_validation.py ../tests/unit/test_cli_config_validate.py ../tests/unit/test_config.py -v --tb=short`; confirm all pass; run `ruff check ../src/coordinare/config_discovery.py ../src/coordinare/config_validation.py ../src/coordinare/__main__.py` and fix any lint warnings; confirm `coordinare config validate --help` and `coordinare --help` output are both correct

**Checkpoint**: All 27 tasks complete. Full test suite passing. Lint clean. SC-001 (500ms), SC-002 (all spec-006 fields), SC-003 (env-only startup), SC-004 (single-pass errors), SC-005 (strict exit 1), SC-006 (structured log) all verified.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Phase 2
- **US2 (Phase 4)**: Depends on Phase 3 (validate_config must exist to extend it)
- **US3 (Phase 5)**: Depends on Phase 4 (discovery integrates into validate_config)
- **US4 (Phase 6)**: Depends on Phase 5 (pre_validate_raw must exist to extend with registry)
- **Polish (Phase 7)**: Depends on Phase 6

### Task-Level Dependencies

| Task | Depends On |
|---|---|
| T002 | T001 |
| T003, T004 | T002 (types must exist to import) |
| T005 | T003 (stubs exist) |
| T006 | T005 (pre_validate_raw must exist) |
| T007 | T006 (validate_config must exist) |
| T008 | T007 (subparser must exist) |
| T009, T010 | T008 (full US1 implementation) |
| T011 | T006 (validate_config to extend) |
| T012, T013 | T011 |
| T014 | T004 (stubs exist) |
| T015 | T014 (implementation to test) |
| T016 | T014 (discover_config_path must exist) |
| T017 | T016 (discovery integrated into validate_config) |
| T018 | T017 (daemon startup updated) |
| T019 | — (populates registry; no blocking prerequisite beyond Phase 2) |
| T020 | T019 (pre_validate_raw reads DEPRECATION_REGISTRY; populate it first) |
| T021 | T020 |
| T022, T023 | T021 |
| T024 | T017, T011 (startup log reads env_var_fields_count from validate_config; T011 must implement it first) |
| T025 | T024 |
| T026 | T001 (config.example.yaml exists) |
| T027 | T025, T026 |

### Parallel Opportunities

Within Phase 2: T003 ‖ T004 (different test files)
Within Phase 3: T008 ‖ (after T007 done, T008 can be written before T009/T010 testing begins)
Within Phase 3: T009 ‖ T010 (different test files, both test T008)
Within Phase 4: T012 ‖ T013 (different test files)
Within Phase 5: T015 ‖ T016 ‖ T017 (after T014; different files)
Within Phase 6: T022 ‖ T023 (after T021; different test files)
Within Phase 7: T025 ‖ T026 (different files)

---

## Parallel Example: User Story 1 (Phase 3)

```bash
# After T006 (validate_config) and T007 (subparser) are complete:

# Parallel group 1:
Task: "T008 [P] Implement _cmd_config_validate stdout rendering in src/coordinare/__main__.py"
# (can proceed independently alongside test writing)

# Parallel group 2 (after T008):
Task: "T009 Fill test_config_validation.py for US1 test cases"
Task: "T010 Fill test_cli_config_validate.py for US1 test cases"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001) — ~15 min
2. Complete Phase 2: Foundational (T002–T004) — ~30 min
3. Complete Phase 3: User Story 1 (T005–T010) — ~2–3 hours
4. **STOP and VALIDATE**: Run `pytest tests/unit/test_cli_config_validate.py -v` — all tests green
5. `coordinare config validate --config config.yaml` produces valid output

### Incremental Delivery

1. Setup + Foundational → skeleton importable
2. US1 → basic `coordinare config validate --config` works
3. US2 → env-var-only startup works
4. US3 → auto-discovery works (no --config needed)
5. US4 → deprecation warnings + --strict work
6. Polish → startup log, docs, lint

Each phase is independently verifiable and delivers meaningful operator value.

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps task to specific user story for traceability
- `extra="ignore"` stays on ProjectConfiguration — unknown field detection is handled by pre_validate_raw, not pydantic (see research.md R2)
- When running tests with monkeypatch for COORDINARE_CONFIG_PATH, be sure to `monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)` in teardown to avoid test interference
- The `_cmd_config_validate` tests can use `capsys` pytest fixture to capture stdout without subprocess overhead
- Conventional commits: `feat:` for new modules, `refactor:` for __main__.py changes, `test:` for test files, `docs:` for config.example.yaml
