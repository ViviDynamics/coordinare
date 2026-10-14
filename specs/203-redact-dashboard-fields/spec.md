# Feature Specification: Redact performer text in every dashboard field

**Feature Branch**: `203-redact-dashboard-fields`
**Created**: 2026-10-14
**Status**: Implemented
**Input**: GitHub issue #203, found in the security re-review of PR #202 (#148).

## Overview

PR #202 (#148) redacts every performer event where it enters state. PR #207 (#132) redacts the live log tail. Several other fields still carry performer, model or command text to dashboard viewers without that redaction: raw performer stderr, open questions (including the local test gate's failing output), observer verdicts, and system error reasons.

The dashboard is watched by people who must not see secrets. The rule is: redact each text field once, where its text enters state, so the snapshot, the SSE stream, the MCP tools, the board comment and the notifications all see the redacted form. Serialisers are not the place to redact.

## Inventory

Every field a viewer can receive (dashboard `/api/state`, the SSE `state_update`, `/api/symphonies`, `/api/symphonies/{name}`, `/api/performer-logs`, `/api/activity`, the run routes, the persona and config routes, and the MCP tools that proxy them) was walked from `dashboard/store.py` (`build_snapshot`, `_snapshot_core`, `_snapshot_extras`, `_session_summary`, `_top_state_summary`, `_serialize_open_questions`, `_serialize_clarifications`, `_session_performer_logs`, `_build_symphonies_data`) and every router under `dashboard/routers/`.

"Before" is the state of `main` before this change.

| Field (viewer path) | Carries | Entry point | Redacted before? | Now |
| --- | --- | --- | --- | --- |
| `performer_logs` (snapshot, session rows, `/api/performer-logs`) | performer stderr | `SubprocessTransport._drain_stderr` (subprocess transport buffer) | No | Redacted as buffered |
| `performer_logs` (HTTP transport) | container log tail | `HTTPPerformerService._redact_tail` | Yes (#207) | Unchanged |
| `open_questions` (snapshot, session rows, board, notifications) | performer and gate output, questions, reasons | writers in `graph/nodes/**`, `services/**` and the `daemon.py` restore (about 100 writes of the text fields in total) | No | Redacted at each writer |
| `card_clarifications` questions and answers (snapshot, session rows, restored sessions) | the questions are performer output copied from `open_questions`; the answers are human issue comments | `check_board`, `monitor/body.py`, `route_issue_comments.py` at write; `daemon.py` restore sites and `state_store.load()` | No (restore and load were unredacted) | The whole list is redacted at each writer and at each restore site, so answers are redacted too. The save path stays raw, because the live state was redacted when it was written |
| Snapshot read from disk (`last_snapshot`, the restored sessions, the v1 synthesis) | any guarded field from a snapshot written before this change | `state_store.load()`, `daemon.py` restore | No | `load()` redacts every guarded field in the whole payload before validating it (`redact_persisted_text`), so `last_snapshot` never holds raw text. The restore sites redact again, which is harmless |
| Refused retune values (`observer_retune_audit`, `observer.retune_applied`, `observer.retune_refused` logs) | the judge's requested knob values, which may be text | `monitor/body.py` `_apply_observer_retune` | No | Redacted before the audit keeps them and before either log event carries them |
| Repair audit `detail` (`repair_audit`, the reviewer record) | reviewer text | `monitor/repair.py` `_repair_record` | No | Redacted in the audit builder, so every record is covered |
| `observer_verdicts` (snapshot, session rows) | judge reason and evidence strings (model text) | `services/observer.py` `parse_verdict`, `_summarise_evidence` | No | Redacted on record |
| `system_error_reason` (board blocked reason, notifications, persisted session; not in the snapshot) | performer output | writers in `graph/nodes/**` | No | Redacted at each writer |
| `env_blocked` (board, notifications, persisted session) | performer cause and reason | `graph/nodes/monitor/body.py` env-blocked phases, `handle_system_error.py` | No | Redacted at each writer |
| `env_health_hold_reason` (board, persisted session) | performer reason | `body.py` env-health phases | No | Redacted at each writer |
| `convergence_reason` (open question text) | judge reason | `body.py` | No | Redacted at each writer |
| `assessor_open_questions` (persisted session) | assessor questions and answers | `body.py`, `check_board.py`, daemon restore | No | Redacted at each writer |
| `last_bootstrap_error` (`/api/symphonies` env cache, snapshot symphony rows) | env bootstrap failure text from the performer | `services/env_cache.py` | No | Redacted at write |
| `last_wiki_init_error` (env cache) | wiki init failure text | `services/wiki_init.py` | No | Redacted at write |
| `last_<role>_error`, e.g. `last_advocate_error`, `last_curator_error` (env cache, persisted with the snapshot) | intake run failure text, from a performer run or the dispatcher's exception | `services/intake_dispatch.py` `register_failure`, called from `daemon.py` and `intake_dispatch.py` | No | Redacted once at the top of `register_failure`, so the stored value and the log both see the redacted form |
| `result_reason` (documenting side and owned writers, persisted) | the writer's reason text | `services/documenting_side.py` `record_result`, `services/owned_writers.py` | No | Redacted at write |
| PR comment body for a rejected baseline repair (`detail`) | the test-integrity guard's reason, which is reviewer text | `graph/nodes/monitor/repair.py` `_reject_repair` (the rejection path) | No | Redacted at the top of the function, before the audit record, the open question and the comment |
| symphony `last_error` (`/api/symphonies`, snapshot symphony rows) | exception text | `daemon.py` symphony failure | No | Redacted at write |
| `session_stats.title` (session rows) | session title generated from the task (model text) | `SubprocessTransport._fetch_session_stats` | No | Redacted at fetch |
| `workflow_step`, `workflow_step_trail` | step names taken from performer event text | `services/workflow_step.py` from the merged events | Yes (#148, events redacted before merge) | Unchanged |
| `performer_events` (session rows) | performer events | `body.py` event merge via `redact_performer_event` | Yes (#148) | Unchanged |
| `activity` feed (`/api/activity`, SSE `activity_event`) | activity text | `ActivityLog.record` via `redact_command_text` | Yes (#148) | Unchanged |
| `/api/runs/...` run records, `download.jsonl`, `download.md`, run log | transcript and run log text | `run_transcript.py`, `run_log.py` at write | Yes (#129, #132) | Unchanged |
| `/api/personas/{role}/history` | persona text versions | `persona_history.py` at write | Yes (#130) | Unchanged |
| `/api/config/*` | operator configuration | config redaction (`config_tools` returns the redacted view) | Operator config, not performer text | Unchanged |
| `last_rebase_round` (snapshot) | structured outcomes (branch, SHAs, outcome) | `check_board.py` | Not free text, except `conflict_preview` which the delegated flow leaves empty | Unchanged |
| `blocked_by_dependencies`, board titles, card titles, `issue_url`, `pr_url` | repository text (issue titles) | GitHub board read | Not performer text | Out of scope, see below |
| `subsystems[].details` (health probes) | probe error text from coordinare's own health checks | `health.py` probes | Not performer text | Out of scope, see below |
| `/api/assistant/message` | the operator's own chat turn | `services/config_assistant.py` | Not state | Out of scope |
| `session_skip_reasons`, `todo_waits`, `pending_control`, `lifecycle_sequence`, `stage_verdicts` | coordinare-written reasons, stage names, markers and SHAs (`stage_verdicts` holds no free text) | coordinare | Not performer text | Unchanged |
| MCP tools `get_state`, `get_performer_logs`, `get_activity`, symphony, config, persona | proxies of the routes above | `mcp/tools/*` through `/api/*` | Follows the routes | Unchanged |

## Coverage gaps (static guard only)

Three fields are written inside large phase functions with no cheap entry point to drive from a test. They are covered by the static writer guard (every write must route through `redact_state_value`), not by the seeded-secret walk:

- symphony `last_error`, written in `daemon.py` `_conduct_single_symphony`.
- `convergence_reason`, written in `graph/nodes/monitor/body.py` `_observer_stall_judgement`.
- `assessor_open_questions`, written in `graph/nodes/monitor/body.py` `_phase_no_progress_relay_s5`.

The daemon mock in the walk reads `symphony_last_error` and `convergence_reason` keys, but the seeding does not set them, so the walk does not exercise those two fields at all.

## Dynamic-write allow-list

Writes whose key the guard cannot resolve to a field name are listed in `_DYNAMIC_WRITE_ALLOWANCES` in the test module, keyed `<file>::<target expression>`, each with a reason. Both maps are keyed `<file>::<function>::<expression>`, so an entry admits a write in its own function and nowhere else in the file. Save-path copies (`_persist_one_session`, `_persist_env_cache`, `_build_snapshot`) are listed in `_SAVE_PATH_COPIES`, keyed by file, function and field. The save path does not redact again, because the live state was redacted by its writers. The restore path and `state_store.load()` do redact, so snapshots written before this change are redacted when they are read.

The `register_failure` entry in `services/intake_dispatch.py` is keyed on `last_*_error`. The error is redacted once at the top of the function, and the `setattr` writes that value.

## Out of scope

- Human text (issue titles, human clarification answers, card titles and board titles) is repository or operator text, not performer or model output. Those remain as GitHub shows them.
- Health probe details come from coordinare's own probes, not from performer output.
- Exact values of secrets that exist only in a job's payload are not known to the daemon at the writer. The redactor's pattern, flag and assignment layers cover the forms a secret takes in text. This is the documented limit already stated in `redact_command_text`.

## Requirements

- **FR-001**: Each writer of a dashboard-visible text field stores the redacted form, so the snapshot, SSE, MCP, the board comment and notifications never hold the raw text.
- **FR-002**: The redaction is applied once, at the writer, through `redact_state_value` (`lib/redaction.py`). The serialisers do not redact.
- **FR-003**: Where a raw copy is needed for a functional reason, the raw copy stays in that one place and the shown copy is redacted. The local test gate's raw failing output still goes into `relay_feedback` so the implementer's self-fix prompt is unchanged. Only the escalated `open_questions` and `system_error_reason` copies are redacted.
- **FR-004**: A guard walks every writer of a text field in the writer modules (`graph`, `services`, `daemon.py`, `cancel.py`) and fails when a value is not routed through the helper.
- **FR-005**: A guard test drives the real entry points with seeded secrets (a `ghp_` token, `export GITHUB_TOKEN=...`, `curl -u user:pass`, `postgresql://u:p@h/db`, an `Authorization: Bearer` header), builds the snapshot, and walks the whole JSON it returns. It also runs the MCP state tool over the same snapshot.
- **FR-006**: Redaction stays linear. `redact_state_value` walks structure and calls the linear `redact_command_text` on each string. No new regex has nested quantifiers.

## Success Criteria

- **SC-001**: No seeded secret value appears anywhere in the snapshot JSON or the MCP state tool output.
- **SC-002**: The writer guard reports zero unwrapped writers in the writer modules. Run against the pre-change code it reports 42 in `body.py` and 11 in `daemon.py` alone.
- **SC-003**: #148 and #207 behaviour is unchanged. Their tests pass, including the linear-time redaction tests.
