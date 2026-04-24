# Data Model: Review Reliability and Dashboard UX Completion

## Backend-side entities

### BackendFormatFailure

Represents a backend output contract failure for JSON-required stages.

| Field | Type | Description |
|-------|------|-------------|
| stage | string | Lifecycle stage where parse failed (`reviewing`, `qa`, etc.) |
| reason | string | Parse failure reason (`empty`, `not_json_object`, etc.) |
| parse_retry_count | int | Number of parse retries consumed |
| max_retries | int | Configured retry budget |
| output_preview | string | Redacted preview of backend output |
| recovery_attempted | bool | Whether structured format-repair path was attempted |

### RoleOutputContract

Defines expected terminal-output schema by role/stage.

| Field | Type | Description |
|-------|------|-------------|
| role | string | Performer role/stage |
| expects_json | bool | Whether stage requires JSON terminal output |
| required_keys | list[string] | Required top-level keys for parsed object |

## Dashboard snapshot extensions

### BoardSummary

Derived from `state["board_snapshot"]` and `state["last_poll_at"]`.

| Field | Type | Description |
|-------|------|-------------|
| todo_count | int | Count of TODO items |
| in_progress_count | int | Count of IN_PROGRESS items |
| in_review_count | int | Count of IN_REVIEW items |
| done_count | int | Count of DONE items |
| blocked_count | int | Count of BLOCKED items (optional display) |
| last_poll_at | string \| null | ISO timestamp of last board poll |

## Frontend runtime entities

### PerformerRoleViewState

Tracks `/performers` list/detail behavior.

| Field | Type | Description |
|-------|------|-------------|
| selected_role | string \| null | Role currently opened in detail view |
| detail_open | bool | Whether detail panel is visible |
| scroll_anchor | string \| null | Optional UI continuity helper for list restore |

### HistoryViewModel

Represents rows rendered on `/history`.

| Field | Type | Description |
|-------|------|-------------|
| timestamp | string | Cycle timestamp |
| phase | string | Cycle phase |
| duration_seconds | number | Cycle duration |
| outcome | string | `success` or `error` |

## Contract impact

- No new external HTTP APIs required.
- SSE snapshot gains additive fields for board summary/last poll exposure.
- Existing `cycle_history` structure remains unchanged and is reused by `/history`.
