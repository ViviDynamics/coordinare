# 043 — Performer CI Ownership — Data Model

## Entities

### CIDetectionResult

Returned by `ci_detection.detect(workspace_path)`. Stateless — no persistence.

| Field | Type | Description |
|---|---|---|
| `lint_command` | `str \| None` | Shell command to run lint/style checks. None if undetectable. |
| `test_command` | `str \| None` | Shell command to run tests. None if undetectable. |
| `stack` | `str` | Detected stack identifier: `"ruby"`, `"python"`, `"node"`, `"make"`, `"unknown"` |
| `detected_from` | `str` | File that triggered detection (e.g. `.rubocop.yml`, `pyproject.toml`) |

### CIRunResult

Returned by the workspace `run_command` helper.

| Field | Type | Description |
|---|---|---|
| `success` | `bool` | True if exit code == 0 |
| `exit_code` | `int` | Process exit code |
| `stdout` | `str` | First 2000 chars of stdout |
| `stderr` | `str` | First 2000 chars of stderr |
| `command` | `str` | The command that was run |
| `duration_seconds` | `float` | Wall-clock time |

## State Changes

No new fields on `CoordinareState` or `WorkflowSnapshot`. The CI gate is a synchronous check within `_advance_stage` that either proceeds (pass) or routes back (fail) — the routing mechanism already exists via the `relay_feedback` / `performer_stage` reset pattern from spec 042.

## Configuration

No new config fields. CI detection is convention-based and runs automatically. Future enhancement (out of scope): `ci_command` override in `config.yaml` for repos with non-standard layouts.
