# Data Model: Async Multi-Card Orchestration Eligibility

## Scheduler Entities

### SessionEligibility

Derived status for each active session in a cycle.

| Field | Type | Description |
|-------|------|-------------|
| card_id | string | Active session card ID |
| eligible | bool | Whether session should be invoked this cycle |
| reason | string | `eligible`, `blocked_column`, `dependency_blocked`, `missing_card`, etc. |
| blockers | list[int] | Dependency issue numbers when `dependency_blocked` |

### AsyncSessionTickResult

Result envelope from one async session invocation.

| Field | Type | Description |
|-------|------|-------------|
| card_id | string | Session card ID |
| ok | bool | Whether graph invocation succeeded |
| error | string \| null | Error details when invocation failed |
| session_state | dict | Updated session state payload |
| duration_ms | number | Invocation duration |

### RebaseDispatchTarget

Derived target for post-merge rebase handling.

| Field | Type | Description |
|-------|------|-------------|
| card_id | string | Active session card ID |
| pr_url | string | Open PR URL for session branch |
| branch | string | Session/workspace branch name |
| eligible | bool | Whether rebase dispatch should run this cycle |
| reason | string | Skip/dispatch reason |

## Snapshot Entity

### SessionSkipReasonMap

Map added to runtime snapshot/state for observability.

| Field | Type | Description |
|-------|------|-------------|
| card_id | string | Active session card ID |
| reason | string | Why it was skipped this cycle |
| detail | string \| null | Optional human-readable detail |
| blockers | list[int] | Optional dependency blockers |

### QAFreshnessCheck

QA contract payload for latest-main validation.

| Field | Type | Description |
|-------|------|-------------|
| latest_main_sha | string | Main SHA observed by coordinare for check context |
| branch_head_sha | string | Branch head SHA evaluated by QA |
| up_to_date | bool | Whether branch includes latest main changes |
| detail | string | Evidence or blocker details |

## Contract Impact

- Additive state/snapshot field(s) only (`session_skip_reasons` map/list).
- Additive QA contract field(s) for freshness validation (`qa_freshness_check`).
- No external API shape breakage required.
- Existing `blocked_by_dependencies` remains authoritative for dependency diagnostics; skip reasons complement it at session scheduling level.
