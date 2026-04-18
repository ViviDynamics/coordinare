# Data Model: Auto-Rebase Active Branches on Merge

## Entities

### RebaseOutcome (enum)

| Value | Description |
|-------|-------------|
| CLEAN | Rebase completed with no conflicts; branch force-pushed |
| PERFORMER_RESOLVED | Rebase had conflicts that the performer resolved; branch force-pushed |
| BLOCKED | Rebase had conflicts the performer could not resolve; card blocked with diagnostics |
| SKIPPED | Branch was already up-to-date or performer was actively running |
| FAILED | Unexpected error (git failure, push rejected, etc.) |

### RebaseJob

Represents a single rebase operation for one in-flight branch.

| Field | Type | Description |
|-------|------|-------------|
| card_id | str | Project item ID (PVTI_...) of the affected card |
| branch | str | Branch name (coordinare/PVTI_.../{slug}) |
| pr_number | int | GitHub PR number |
| pre_rebase_sha | str | HEAD SHA before the rebase |
| post_rebase_sha | str | HEAD SHA after the rebase (empty if failed) |
| target_main_sha | str | The main HEAD SHA being rebased onto |
| outcome | RebaseOutcome | Result of the rebase operation |
| conflicted_files | list[str] | File paths with conflicts (empty if clean) |
| conflict_preview | str | Truncated conflict content for diagnostics (empty if clean) |
| duration_seconds | float | Wall-clock time for the rebase operation |

### RebaseRound

The set of RebaseJobs triggered by a single merge event.

| Field | Type | Description |
|-------|------|-------------|
| trigger_pr_number | int | PR number that was merged to trigger this round |
| trigger_sha | str | The new main HEAD SHA after the merge |
| timestamp | datetime | When the rebase round started |
| jobs | list[RebaseJob] | One per in-flight branch |

## State Extensions

### CoordinareState additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| last_known_main_sha | str or None | None | The main branch HEAD SHA as of the last poll cycle. Used to detect external merges (non-coordinare PRs merged by humans). Updated after each successful poll + rebase round. |
| last_rebase_round | dict or None | None | Serialized RebaseRound from the most recent merge event. Used by the dashboard to show per-card rebase status. Cleared when the next merge occurs. |

### Card dict additions (transient, not persisted)

| Field | Type | Description |
|-------|------|-------------|
| rebase_status | dict or None | `{outcome, timestamp, new_sha}` — set by the rebase service for dashboard display. Cleared on next dispatch. |

## State Transitions

```
RebaseJob lifecycle:

  [merge detected]
       │
       ├── branch up-to-date? ──→ SKIPPED
       ├── performer running? ──→ SKIPPED (try next round)
       │
       └── git rebase origin/main
              │
              ├── clean exit ──→ force-push-with-lease ──→ CLEAN
              │                                    └── push rejected ──→ retry once ──→ FAILED
              │
              └── conflict exit
                     │
                     ├── dispatch performer ──→ resolved ──→ force-push ──→ PERFORMER_RESOLVED
                     │
                     └── performer failed/blocked ──→ BLOCKED (post diagnostic comment)
```

## Relationships

```
RebaseRound ──── trigger_sha ──→ Main HEAD commit
     │
     └── jobs[] ──→ RebaseJob ──── card_id ──→ Active Session
                        │
                        └── branch ──→ Git branch (coordinare/PVTI_.../{slug})
```
