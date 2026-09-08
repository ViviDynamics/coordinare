# Phase 1 Data Model

Three groups: what a run produces (performer-side records), what coordinare
remembers between runs (persisted state), and what an operator configures.

## Performer-side: the advocate

### `IssueCandidate`

One open issue the run is considering. Built by the intake step, never by the
model.

| Field | Type | Notes |
|---|---|---|
| `issue_id` | `str` | GraphQL node id, required, non-empty |
| `number` | `int` | issue number, required |
| `title` | `str` | may be empty |
| `body` | `str` | may be empty |
| `url` | `str` | for the escalation notification |
| `labels` | `list[str]` | as fetched, used to skip already-handled issues |

### `DocumentRead`

A documentation file the run actually read. The gate's allow-list is exactly the
set of these with `read=True`.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | repository-relative, as configured |
| `content` | `str` | empty when unread |
| `read` | `bool` | False when the file was absent, matching today's tolerance |

### `Classification`

The model's judgement for one issue. One schema-guarded call produces one of
these. The schema **forbids** severity, routing and any verdict field, exactly as
the reviewer's findings schema forbids a verdict: the model classifies, the code
decides.

| Field | Type | Validation |
|---|---|---|
| `issue_id` | `str` | must be an id the run sent, else discarded |
| `classification` | enum | `question`, `confusion`, `complaint`, `feature_request`, `bug_report`, `off_topic` |
| `confidence` | `float` | clamped to 0.0 through 1.0 |
| `reasoning` | `str` | required, non-empty |
| `answer` | `str \| None` | non-null only for `question` and `confusion` |
| `cited_documents` | `list[str]` | paths the answer claims; each must match a `DocumentRead` with `read=True` |

### `IssueOutcome`

What the run decided and did for one issue. Produced by the gate and the act
step, never by the model.

| Field | Type | Notes |
|---|---|---|
| `issue_id` | `str` | |
| `action` | enum | `replied`, `acknowledged`, `triaged`, `redirected`, `escalated` |
| `escalation_reason` | `str \| None` | set only when `action="escalated"` |
| `classification` | enum `\| None` | absent when escalated before any model call |
| `cited_documents` | `list[str]` | the surviving citations, for the record |
| `label_applied` | `str` | the label actually applied |
| `comment_posted` | `bool` | |
| `model_calls` | `int` | zero for a keyword escalation |

### `AdvocateRecord`

The run's report. Serialised under the report key `advocate`.

| Field | Type | Notes |
|---|---|---|
| `verdict` | enum | `advocate_complete`, `env_blocked` |
| `issues_seen` | `int` | after the already-handled skip |
| `documents_read` | `list[str]` | paths with `read=True` |
| `outcomes` | `list[IssueOutcome]` | one per issue processed |
| `withheld` | `list[dict]` | answers the gate refused, with the reason, so a refusal is visible rather than silent |
| `model_calls` | `int` | run total |
| `write_free_check` | `str` | the executed `git status --porcelain` output, proving no commit |

**State transitions per issue**: `candidate` → (`sensitive keyword` → `escalated`,
zero calls) or (`classified` → gate → `replied` / `acknowledged` / `triaged` /
`redirected` / `escalated`). There is no path from `classified` to `replied` that
skips the gate.

## Performer-side: the curator

### `SelectionJudgement`

One guarded judgement per candidate.

| Field | Type | Validation |
|---|---|---|
| `issue_id` | `str` | must be an id the run sent, else discarded |
| `qualifies` | `bool` | |
| `reason` | `str` | required, non-empty |
| `quote` | `str` | must appear verbatim in that issue's title or body, else the judgement is rejected |

### `CurationOutcome`

| Field | Type | Notes |
|---|---|---|
| `issue_id` | `str` | |
| `action` | enum | `added`, `skipped`, `rejected` |
| `reason` | `str` | why it qualified, or why the judgement was rejected |
| `board_item_id` | `str \| None` | set only on `added` |
| `column` | `str \| None` | the backlog column, never the dispatch column |

### `CurationRecord`

Serialised under the report key `curation`.

| Field | Type | Notes |
|---|---|---|
| `verdict` | enum | `curation_complete`, `env_blocked` |
| `candidates_seen` | `int` | after the already-on-board filter |
| `outcomes` | `list[CurationOutcome]` | |
| `rejected_judgements` | `list[dict]` | id and why, so an unquotable reason is visible |
| `model_calls` | `int` | |
| `write_free_check` | `str` | |

## Coordinare-side: persisted state

Added to `EnvCacheState`, with the persisted subset mirrored onto
`EnvCacheStateSnapshot`. `CURRENT_SCHEMA_VERSION` bumps 19 to 20. Snapshots
from v1 to v19 load with these defaults and need no migration.

Per role, with `<role>` being `advocate` and `curator`:

| Field | Type | Default | Persisted | Notes |
|---|---|---|---|---|
| `<role>_in_flight` | `bool` | `False` | **no** | transient by design, so a crash cannot wedge the role. Set before dispatch, rolled back on a synchronous raise |
| `<role>_attempts` | `int` | `0` | yes | the breaker's counter, reset on success |
| `<role>_exhausted` | `bool` | `False` | yes | the breaker tripped, stop trying |
| `last_<role>_run_at` | `datetime \| None` | `None` | yes | drives the cooldown; timezone-aware |
| `last_<role>_succeeded` | `bool \| None` | `None` | yes | |
| `last_<role>_error` | `str \| None` | `None` | yes | the reason a run failed |
| `last_<role>_issues_seen` | `int` | `0` | yes | so an operator can see the last run did something |

Deliberately **not** persisted anywhere: which issues have been handled. That is
read from the issue's labels and, for the curator, from the issue's presence on
the board. The in-memory `advocate_history` set retires with the service that
owned it.

## Coordinare-side: configuration

### `AdvocateConfig`, trimmed

Kept, because the run still needs them: `enabled`, `github_repo`,
`confidence_threshold`, `sensitive_keywords`, `doc_sources`, `doc_branch`,
`handled_label`, `escalation_label`, `holding_comment_template`,
`acknowledgement_template`, `redirect_template`, `disclosure_template`,
`support_channel_url`.

Removed: `scoring_models`, which nothing reads today and which described the
multi-provider path that retires with `services/scoring.py`.

Added: `scan_interval_seconds`, the cooldown floor between runs, with a default
well above the poll interval.

### `CuratorConfig`, new

| Field | Default | Notes |
|---|---|---|
| `enabled` | `False` | the role does nothing until switched on |
| `github_repo` | `""` | required non-empty when enabled, validated at load as the advocate's is |
| `label` | `"curator-proposed"` | applied to a promoted issue |
| `backlog_column` | `"Backlog"` | must not be the column coordinare dispatches from, validated at load |
| `criteria` | the three shipped criteria | clear acceptance criteria, single concern, no unresolved blockers |
| `scan_interval_seconds` | as the advocate's | |
| `max_per_run` | small, non-zero | a bound on how many issues one run may promote |

### Personas

`DEFAULT_INSTRUCTIONS["advocate"]` is rewritten to the responder job.
`DEFAULT_INSTRUCTIONS["curator"]` is added, describing selection, and is where
the board-curation text that was wrongly attached to the advocate finally
belongs. Both are added to `VALID_ROLES` so they remain editable, and
`PersonasConfig` gains a `curator` field.

## Score

One new field, because nothing conveys it today and the add-to-board call
silently no-ops without it:

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id` | `str` | `""` | the board's node id. Empty means the curator cannot add and must report that rather than appearing to succeed |
