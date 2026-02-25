# Implementation Plan: Configuration Management

**Branch**: `008-config-management` | **Date**: 2026-02-25 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/008-config-management/spec.md`

---

## Summary

Adds a `coordinare config validate` CLI subcommand that validates the resolved configuration (YAML file + env vars) against the current schema in a single pass, reporting all errors, type mismatches, unknown fields, and deprecation warnings without starting the daemon. Implements automatic multi-location config discovery (4-path search order), scalar env var overrides via `COORDINARE_` prefix (pydantic-settings native), and a static `DEPRECATION_REGISTRY` that maps the seven fields removed in spec 006 to their replacement paths and migration instructions. The existing `ProjectConfiguration(BaseSettings)` model is retained; all new behaviour is layered via a pre-validation function (`pre_validate_raw`) and a discovery function (`discover_config_path`), keeping the model interface unchanged.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `pydantic-settings` (existing), `pydantic` v2 (existing), `structlog` (existing), `argparse` (stdlib — existing)
**Storage**: N/A (config is loaded from YAML file or env vars; no persistence)
**Testing**: `pytest` (existing), `pytest-monkeypatch` for env var injection (existing)
**Target Platform**: Linux server (coordinare daemon host), macOS (developer workstation)
**Project Type**: Single project (src/ + tests/)
**Performance Goals**: `coordinare config validate` completes in < 500ms (SC-001); effectively instant — no network I/O, no daemon startup
**Constraints**: No new external dependencies; stdlib + existing packages only (Constitution Principle I: Minimal dependencies)
**Scale/Scope**: Single config file; < 50 top-level fields; negligible scale concerns

---

## Constitution Check

### Principle I — Code Quality First ✅

- All new modules (`config_discovery.py`, `config_validation.py`) have a single clear purpose.
- Type annotations on all public interfaces.
- No dead code — deprecated field list is a live registry, not commented-out stubs.

### Principle II — Testing Discipline ✅

- `test_config_discovery.py`: unit tests for all discovery paths (explicit, env var, cwd, home, none)
- `test_config_validation.py`: unit tests for pre-validation pass (deprecated fields, unknown fields, empty string handling, env var type error)
- `test_cli_config_validate.py`: tests for CLI exit codes (0/1) and stdout content
- `test_config.py`: existing tests updated to reflect new validation path; no regressions
- Performance budget (SC-001) validated with a timing assertion in the CLI test

### Principle III — UX Consistency ✅

- Error messages include field path + error type + fix hint (FR-009)
- Consistent `[TYPE] field_name` bracket format across all error/warning output
- Output goes to stdout (not stderr) for easy CI capture

### Principle IV — Performance by Design ✅

- SC-001 (< 500ms) is measurable; CLI test includes `time.perf_counter()` assertion
- No daemon startup, no network calls in the validate path

### Principle V — Clarity Before Action ✅

- All two clarification questions answered and recorded in spec.md
- No `NEEDS CLARIFICATION` tags remain anywhere in this feature's artifacts

### Quality Gates

| Gate | Status |
|---|---|
| Lint & Format (ruff) | Required; enforced in CI |
| Type Check | Required; all new code fully typed |
| Unit Tests | Required; three new test files |
| Integration Tests | N/A (no external service boundaries) |
| Coverage | Must not decrease |
| Performance | SC-001 assertion in CLI test |
| Accessibility | N/A (CLI, no UI) |
| Code Review | Required per constitution |

**No violations. No Complexity Tracking entries needed.**

---

## Project Structure

### Documentation (this feature)

```text
specs/008-config-management/
├── plan.md                          # This file
├── research.md                      # Phase 0 output
├── data-model.md                    # Phase 1 output
├── quickstart.md                    # Phase 1 output
├── contracts/
│   └── cli-config-validate.md       # CLI contract (exit codes, stdout format, coverage)
└── tasks.md                         # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                        # MODIFIED — update from_yaml() integration seam
├── config_discovery.py              # NEW — discover_config_path()
├── config_validation.py             # NEW — DEPRECATION_REGISTRY, validate_config(), types
└── __main__.py                      # MODIFIED — add 'config validate' subcommand + startup log

tests/unit/
├── test_config.py                   # MODIFIED — update helper; existing tests must pass unchanged
├── test_config_discovery.py         # NEW
├── test_config_validation.py        # NEW
└── test_cli_config_validate.py      # NEW
```

---

## Design Decisions

### DD-1: Pre-validation layer (not `extra="forbid"`)

**Problem**: The existing `extra="ignore"` on `ProjectConfiguration` silently discards unknown YAML keys. Changing to `extra="forbid"` would cause pydantic-settings to reject extra env vars with the `COORDINARE_` prefix that are not in the schema (e.g., `COORDINARE_ANTHROPIC_API_KEY`), which is a valid pattern operators may use.

**Solution**: Keep `extra="ignore"` on the pydantic model. Add a `pre_validate_raw(raw: dict)` function that operates on the raw YAML dict (file-sourced only) before pydantic model instantiation. This function detects:
1. Keys in `DEPRECATION_REGISTRY` → `ConfigDeprecationWarning`
2. Keys not in `ProjectConfiguration.model_fields` and not deprecated → `ConfigFieldError(unknown_field)`

Pydantic handles everything else (missing fields, type errors, validator failures).

**Trade-off**: Top-level unknown field detection only covers YAML-sourced keys, not env var names. A typo in an env var name (e.g., `COORDINARE_GITHUB_TOKE=...`) silently has no effect — this is the same behaviour as before spec 008 and is acceptable. Env var name validation is out of scope.

---

### DD-2: `from_yaml()` integration seam

**Problem**: The existing `from_yaml(path)` method loads the raw dict and calls `cls(**raw)`. The pre-validation pass needs to operate on the same raw dict before `cls(**raw)` runs.

**Solution**: Extract raw dict loading into a helper `_load_raw_yaml(path: Path) -> dict`. Both `from_yaml()` and `validate_config()` call this helper. No change to the public `from_yaml()` interface.

---

### DD-3: Env var field count for startup log

**Problem**: SC-006 requires the startup log to include `env_var_fields_count` — the number of scalar fields resolved from env vars.

**Solution**: After successful model instantiation, compare the resolved model field values against the raw YAML dict values. A field is counted as "from env var" if:
- It is absent from the raw YAML dict, OR
- Its resolved value differs from the raw YAML value

This is an approximation (correct for all scalar fields; list fields are excluded since they cannot be env-var-sourced per A-002).

---

### DD-4: Closest field suggestion for unknown fields

**Problem**: FR-009 requires a "closest valid field name" suggestion for unknown fields (e.g., `githubb_token` → `github_token`).

**Solution**: Use Python stdlib `difflib.get_close_matches(unknown_key, model_fields, n=1, cutoff=0.6)`. If a match is found, include it in the fix hint. If not, omit the suggestion. No external dependency needed.

---

### DD-5: CLI subcommand via argparse subparsers

**Problem**: Current `__main__.py` uses a flat argparse parser with no subcommands.

**Solution**: Add `subparsers = parser.add_subparsers(dest="command")`. Register a `config` subparser; under it register a `validate` subparser. The top-level parser retains all existing `--config`, `--log-level`, `--structured-output` flags. When `command` is `None` (no subcommand), run the daemon as before. When `command` is `"config"` and action is `"validate"`, run the validation handler.

**Dispatch pattern**:
```python
args = parser.parse_args()
if args.command == "config" and args.config_action == "validate":
    _cmd_config_validate(args)
else:
    main_daemon(args)
```

---

## Implementation Phases

### Phase A — Foundation (no-impact)

Create new modules without modifying any existing code. Existing behaviour is unchanged.

**A1**: Create `src/coordinare/config_discovery.py`
- `ConfigDiscoveryError(Exception)` — raised when an explicit or env-specified path cannot be found
- `discover_config_path(explicit: Path | None) -> Path | None` — implements the 4-path search order

**A2**: Create `src/coordinare/config_validation.py`
- `DeprecationEntry` dataclass (removed_in, replacement_path, migration_hint)
- `ConfigDeprecationWarning` dataclass (field_name, removed_in, replacement_path, migration_hint)
- `ErrorType` enum (missing, wrong_type, unknown_field, invalid_value)
- `ConfigFieldError` dataclass (field_path, error_type, fix_hint, source)
- `ConfigValidationResult` dataclass (errors, warnings, config_file_path, env_var_fields_count, passed)
- `DEPRECATION_REGISTRY: dict[str, DeprecationEntry]` — 7 spec-006 entries
- `pre_validate_raw(raw: dict) -> tuple[list[ConfigFieldError], list[ConfigDeprecationWarning]]`
- `validate_config(config_path: Path | None, strict: bool = False) -> ConfigValidationResult`

**A3**: Write `tests/unit/test_config_discovery.py`
- `test_explicit_path_used_when_provided`
- `test_explicit_path_error_when_not_found`
- `test_explicit_path_error_when_directory`
- `test_env_var_config_path_used`
- `test_env_var_config_path_error_when_not_found`
- `test_env_var_config_path_error_when_directory`
- `test_cwd_config_yaml_found`
- `test_home_config_found_when_cwd_absent`
- `test_none_returned_when_no_file_found`

**A4**: Write `tests/unit/test_config_validation.py`
- `test_deprecated_field_raises_warning`
- `test_all_spec006_fields_in_registry`
- `test_unknown_field_raises_error`
- `test_unknown_field_suggests_closest_match`
- `test_truly_unknown_field_no_suggestion`
- `test_deprecated_field_not_treated_as_unknown`
- `test_missing_required_field_error`
- `test_type_mismatch_error`
- `test_empty_string_env_var_treated_as_absent`
- `test_all_errors_collected_in_single_pass`
- `test_valid_config_passes`
- `test_env_var_fields_count_computed`

### Phase B — CLI Integration

Wire the new modules into the CLI and daemon startup.

**B1**: Modify `src/coordinare/__main__.py`
- Refactor `_build_arg_parser()` to add subparsers
- Add `_cmd_config_validate(args)` handler:
  - Calls `discover_config_path(args.config)` → catches `ConfigDiscoveryError` → prints error, exit 1
  - Calls `validate_config(config_path, strict=args.strict)`
  - Renders `ConfigValidationResult` to stdout (errors, then warnings)
  - Exits 0 or 1 based on result
- Update `main()` to dispatch on `args.command`
- Update daemon config loading to use `discover_config_path()` + `validate_config()` instead of bare `from_yaml()`
- Add structured startup log entry (`event="config_loaded"`) after successful validation

**B2**: Write `tests/unit/test_cli_config_validate.py`
- `test_valid_config_exits_zero`
- `test_missing_field_exits_one`
- `test_unknown_field_exits_one`
- `test_deprecated_field_exits_zero_without_strict`
- `test_deprecated_field_exits_one_with_strict`
- `test_no_config_file_all_env_vars_exits_zero`
- `test_no_config_file_missing_fields_exits_one_with_searched_paths`
- `test_explicit_path_not_found_exits_one`
- `test_all_errors_reported_in_single_pass`
- `test_validate_completes_under_500ms` (SC-001)

### Phase C — Existing Test Compatibility

**C1**: Update `tests/unit/test_config.py`
- Update `_write_config()` helper if needed (deprecated fields may be removed from model after spec 006)
- Verify all existing tests pass without modification (the `from_yaml()` interface is unchanged)
- Add `test_daemon_startup_emits_config_loaded_log` (structlog caplog fixture)

---

## Dependency Order

```
A1 (discovery) ──→ A3 (discovery tests)
A2 (validation) ──→ A4 (validation tests)
A1 + A2 ──→ B1 (CLI integration)
B1 ──→ B2 (CLI tests)
B1 ──→ C1 (compatibility tests)
```

All of A can be done in parallel. B depends on A. C depends on B.

---

## Out of Scope

- Config file merging (combining multiple files) — explicitly deferred, A-003
- XDG base directory support — stretch goal for a later spec
- External secret stores (AWS SSM, Vault) — deferred, A-004
- Auto-migration of deprecated fields — report-only per A-006
- Env var override for list-type fields (e.g., `notifications.channels`) — scalar-only per A-002 / clarification Q2
- New top-level binary — subcommand of existing `coordinare` entry point per A-005
