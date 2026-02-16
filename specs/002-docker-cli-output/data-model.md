# Data Model: Runtime Execution and Output Visibility

**Feature**: 002-docker-cli-output  
**Date**: 2026-02-16  
**Source**: `~/Workspaces/ViviDynamics/coordinare/specs/002-docker-cli-output/spec.md`

## Entities

### 1. RuntimeExecution

Represents one daemon execution instance.

| Field | Type | Required | Description |
|------|------|----------|-------------|
| `execution_id` | string | Yes | Unique runtime identifier |
| `run_mode` | enum(`shell`,`compose`) | Yes | Invocation mode used |
| `started_at` | datetime | Yes | Startup timestamp |
| `ended_at` | datetime | No | Completion timestamp |
| `status` | enum(`starting`,`running`,`failed`,`stopped`) | Yes | Current lifecycle state |
| `exit_code` | integer | No | Process/container exit code |
| `config_path` | string | Yes | Effective runtime config path |
| `structured_output_enabled` | boolean | Yes | Whether structured output mode is active |
| `log_level` | enum(`debug`,`info`,`warning`,`error`) | Yes | Active verbosity level |

**Validation rules**:
- `execution_id` must be unique per run.
- `run_mode` must be one of `shell` or `compose`.
- `exit_code` is required when `status` is terminal (`failed` or `stopped`).
- `exit_code` must be non-zero when `status=failed`.

### 2. RuntimeOutputEvent

Represents one user-visible output/log event.

| Field | Type | Required | Description |
|------|------|----------|-------------|
| `event_id` | string | Yes | Unique event identifier |
| `execution_id` | string | Yes | Parent runtime execution |
| `timestamp` | datetime | Yes | Event time |
| `category` | enum(`startup`,`heartbeat`,`activity`,`state_change`,`failure`,`shutdown`) | Yes | Event classification |
| `level` | enum(`debug`,`info`,`warning`,`error`) | Yes | Event level |
| `message` | string | Yes | Human-readable message |
| `structured_data` | object | No | Optional structured payload |
| `redaction_applied` | boolean | Yes | Whether known sensitive fields were masked |

**Validation rules**:
- `message` must be non-empty.
- `category=failure` events must include failing-step context.
- `structured_data` may only be present when structured mode is enabled.

### 3. RuntimeConfigAsset

Represents configuration artifacts for project runtime.

| Field | Type | Required | Description |
|------|------|----------|-------------|
| `asset_name` | string | Yes | Config artifact name |
| `asset_type` | enum(`example`,`local`) | Yes | Template vs local runtime file |
| `path` | string | Yes | Relative repository path |
| `git_ignored` | boolean | Yes | Whether excluded from git tracking |
| `docker_ignored` | boolean | Yes | Whether excluded from docker build context |

**Validation rules**:
- `example` assets should be committed (`git_ignored=false`, `docker_ignored=false`).
- `local` assets must be ignored in both contexts (`git_ignored=true`, `docker_ignored=true`).

## Relationships

- `RuntimeExecution` 1..* `RuntimeOutputEvent`
- `RuntimeExecution` uses 1..* `RuntimeConfigAsset`

## State Transitions

### RuntimeExecution Lifecycle

`starting -> running -> stopped`

`starting -> failed`

`running -> failed`

### Transition Constraints

- Runtime errors move state to `failed` and produce non-zero exit code.
- Terminal transitions produce `shutdown` event with completion context.
