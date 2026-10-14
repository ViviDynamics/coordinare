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
| `card_clarifications` questions (snapshot) | the questions above, copied from `open_questions` | `check_board` copy of `open_questions` | Derived from a redacted source | Unchanged |
| `card_clarifications` answers (snapshot) | human issue comments | `route_issue_comments` | Not performer text | Out of scope, see below |
| `observer_verdicts` (snapshot, session rows) | judge reason and evidence strings (model text) | `services/observer.py` `parse_verdict`, `_summarise_evidence` | No | Redacted on record |
| `system_error_reason` (board blocked reason, notifications, persisted session; not in the snapshot) | performer output | writers in `graph/nodes/**` | No | Redacted at each writer |
| `env_blocked` (board, notifications, persisted session) | performer cause and reason | `graph/nodes/monitor/body.py` env-blocked phases, `handle_system_error.py` | No | Redacted at each writer |
| `env_health_hold_reason` (board, persisted session) | performer reason | `body.py` env-health phases | No | Redacted at each writer |
| `convergence_reason` (open question text) | judge reason | `body.py` | No | Redacted at each writer |
| `assessor_open_questions` (persisted session) | assessor questions and answers | `body.py`, `check_board.py`, daemon restore | No | Redacted at each writer |
| `last_bootstrap_error` (`/api/symphonies` env cache, snapshot symphony rows) | env bootstrap failure text from the performer | `services/env_cache.py` | No | Redacted at write |
| `last_wiki_init_error` (env cache) | wiki init failure text | `services/wiki_init.py` | No | Redacted at write |
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
