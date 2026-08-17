# Feature Specification: Attempt Telemetry

**Feature Branch**: `141-attempt-telemetry`
**Created**: 2026-08-03
**Status**: Draft
**Input**: Every time a performer takes a card through to a verdict, coordinare discards the attempt — which model ran it, how long it took, how many tokens it used, what the verdict was. This spec instruments the graph to write one structured row per attempt to an append-only JSONL log. That log is the single source of truth for bounce counts, benchmark results, and the future routing dataset.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Attempt Records Written End-to-End (Priority: P1)

As a data scientist, I want a structured log row written for every performer attempt so that I can query attempt history with `pd.read_json("logs/attempts/...", lines=True)` without grepping raw log files.

**Why this priority**: Without persisted attempt records, all other workstreams (benchmarking, routing) are impossible. This is the data foundation everything else builds on.

**Independent Test**: Run coordinare against a real card through to a terminal state. Verify: (a) a JSONL file exists under the configured `attempt_log_dir`; (b) globbing the directory and loading all rows, the card's attempt rows are present with the expected count; (c) each row is valid JSON conforming to schema v1; (d) every row parses cleanly with `json.loads`.

**Acceptance Scenarios**:

1. **Given** `dispatch_card` dispatches a card to a performer, **When** the graph transitions out of `dispatch_card`, **Then** an AttemptRecord start row is written to the JSONL log with `started_at`, `attempt_id`, `task_id`, `model_tier`, `model_name`, and `routing_reason` populated; `ended_at`, `verdict`, and `terminal_state` are `null`.
2. **Given** `merge_pr` successfully merges a card, **When** the merge completes, **Then** an AttemptRecord end row is written with `verdict`, `verdict_source`, `terminal_state: "merged"`, `ended_at`, `wall_time_s`, `task_id`, and `started_at` all populated. `verdict` is read from `PersistedSession.stage_verdicts` where `monitor_agent` saved it.
3. **Given** `handle_blocked` reaches the true block path, **When** the block is recorded, **Then** an AttemptRecord end row is written with `terminal_state: "blocked"`, `verdict`, `verdict_source`, `ended_at`, and `wall_time_s` populated.
4. **Given** `handle_system_error` handles an infra failure, **When** the error is recorded, **Then** an AttemptRecord end row is written with `verdict: "error"` or `verdict: "timeout"` and `terminal_state: null`.
5. **Given** a card bounces and is re-dispatched, **When** `dispatch_card` runs for the second time on the same card, **Then** a new AttemptRecord start row is written with a new `attempt_id`, the same `task_id`, and `parent_attempt_id` set to the previous attempt's `attempt_id`.

---

### User Story 2 — Bounce Count Derivable from Records (Priority: P2)

As an operator, I want to derive how many times card X bounced before merging so I can identify consistently difficult cards without relying on a separate counter that can drift.

**Why this priority**: The `bounce_counter` in state is ephemeral — it disappears when the card's session ends. AttemptRecords make bounce count a query: `count(rows where task_id=X and verdict="fail")`.

**Independent Test**: Process a card that bounces twice then merges. Load the JSONL. Verify: 6 rows share the same `task_id` (3 start rows, 3 end rows); among the end rows, two have `verdict: "fail"` and one has `verdict: "pass"` and `terminal_state: "merged"`; bounce count derived from the log matches the `bounce_counter` value observed during the run.

**Acceptance Scenarios**:

1. **Given** a card has bounced N times, **When** the JSONL is queried for `task_id = card.id` with `verdict = "fail"`, **Then** exactly N rows are returned.
2. **Given** a card merges on its first attempt, **When** the JSONL is queried for that `task_id`, **Then** exactly one row exists with `verdict: "pass"` and `terminal_state: "merged"`.
3. **Given** a system error occurs (Docker failure, transport timeout), **When** that attempt's record is inspected, **Then** `verdict` is `"error"` or `"timeout"` — never `"fail"` — so infra failures are not counted as evidence of task difficulty.

---

### User Story 3 — Spec Fields Captured at Decision Time (Priority: P3)

As a researcher building a routing model, I want all `spec_*` fields captured at dispatch time so that router features are never contaminated by information that only exists after the attempt completes.

**Why this priority**: Leaking outcome-side information into training features produces a model that cannot generalize to new cards. Snapshotting at dispatch time is the hard leakage rule.

**Independent Test**: Change the card spec body after dispatch. Verify that the AttemptRecord's `spec_word_count` reflects the word count at dispatch time, not the modified body.

**Acceptance Scenarios**:

1. **Given** a card is dispatched, **When** the AttemptRecord is written at attempt-start, **Then** all `spec_*` fields are snapshotted from the card's description at that moment.
2. **Given** a card body is subsequently edited on GitHub, **When** a later attempt on the same card is dispatched, **Then** the new attempt's `spec_*` fields reflect the updated body — each attempt snapshots independently.

---

### Edge Cases

- What if `dispatch_card` fails before writing the attempt-start record? The record is never written — a partial attempt that never started produces no row. This is correct; a failed dispatch is not a performer attempt.
- What if the JSONL write fails (disk full, permissions)? Log a warning via structlog and continue — telemetry failure must never block card processing.
- What if a card is abandoned mid-attempt (daemon restart, wedge recovery)? The open record (null `ended_at`) remains in the log as an incomplete row. Consumers must handle null `ended_at`. A reconciliation pass (T-A4 backfill) may close these rows with `verdict: "error"`.
- What if two cards are processed in parallel? Each card's session writes to the same daily JSONL file. Writes are safe because coordinare is single-threaded asyncio (A-002) — only one coroutine executes at a time — and the file is opened in `O_APPEND` mode with a single `write()` call per row that is well under PIPE_BUF (4KB on Linux), making each row atomically either fully written or not written at all. Note: the GIL does NOT guarantee this — it serializes bytecode, not `write()` calls against a shared buffer. Two coordinare processes appending to the same log directory is out of contract; row interleaving would corrupt the JSONL.
- What if an attempt spans midnight? The log path is snapshotted at `open_attempt` time and stored in memory. `close_attempt` uses the stored path, so both rows land in the same file regardless of what day it is when the attempt ends. Consumers must still glob the full directory because historical rows span many daily files.
- What if the model name is unavailable at dispatch time (proxy routing, dynamic selection)? Write `model_name: null` at attempt-start; update at attempt-end when the value is known from the performer's response.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: At attempt-start (`dispatch_card`), coordinare MUST write an AttemptRecord row to `{attempt_log_dir}/YYYY-MM-DD.jsonl` (date in UTC) with all decision-time fields populated and outcome fields set to `null`. The log directory MUST be derived from config (using the existing `performer_log_dir` convention) rather than a bare relative path, so rows always land in a predictable location regardless of the daemon's working directory. The log path MUST be snapshotted at attempt-start and reused at attempt-end so that midnight-spanning attempts (start row and end row written on different calendar dates) always land in the same file. Consumers MUST glob the directory rather than reading a single file. Log retention and rotation are out of scope for this spec.
- **FR-002**: At attempt-end, coordinare MUST append a second row with the same `attempt_id`, all outcome fields populated, and `task_id` and `started_at` copied from the start row. The end row carries both `verdict` and `terminal_state` together. Close sites: `merge_pr` (terminal_state="merged"), `handle_blocked` true-block path (terminal_state="blocked"), and `handle_system_error` (verdict="error" or "timeout", terminal_state=null). Writing the end row at the terminal node ensures a single end row per attempt with no third row needed. Note for implementers: the registered graph node key is `monitor_agent` (`builder.py:43`); `monitor_performer` is the underlying function but `monitor_agent` is the correct instrumentation target.
- **FR-003**: The JSONL file MUST be append-only. Coordinare MUST NOT modify or delete existing rows. Each row MUST be valid JSON terminated by a newline (`\n`).
- **FR-004**: Every row MUST include `schema_version: 1`. Future schema changes increment this field; consumers can filter by version.
- **FR-005**: Coordinare MUST NOT write secrets, raw card bodies, or raw PR descriptions to the log. Card identity is recorded as `task_id` (the GitHub card ID string). Spec content is recorded only as derived scalars (`spec_word_count`, `spec_has_acceptance_tests`). Diff content, if recorded in a future revision, MUST be stored as a URI only — never inline.
- **FR-006**: All `spec_*` fields MUST be snapshotted from the card's description at dispatch time (attempt-start), not at verdict time.
- **FR-007**: The `verdict` field MUST use exactly the taxonomy: `pass` | `fail` | `error` | `timeout`. `error` and `timeout` MUST NOT be used as `fail`. Consumers filtering for routing labels MUST exclude `error` and `timeout` rows.
- **FR-008**: A re-dispatched card (bounce) MUST produce a new AttemptRecord row with a new `attempt_id` and `parent_attempt_id` set to the previous attempt's `attempt_id`. To survive daemon restarts (bounces routinely span them), `dispatch_card` MUST write the returned `attempt_id` to `PersistedSession.last_attempt_id` immediately after calling `open_attempt`. On re-dispatch, it reads `last_attempt_id` from state and passes it as `parent_attempt_id`. This requires a new field on `PersistedSession` and a `CURRENT_SCHEMA_VERSION` bump in `state_store.py`; old sessions without the field default to `last_attempt_id: null`.
- **FR-009**: `logs/attempts/` MUST be listed in `.gitignore`. Attempt logs are operational data, not source artifacts.
- **FR-010**: A JSONL write failure MUST be caught, logged as a structlog warning, and MUST NOT propagate as an exception that interrupts card processing.
- **FR-011**: An automated golden test MUST verify that a known end-to-end card run produces exactly the expected AttemptRecord rows with correct field values. Each row MUST be validated with `json.loads` and checked against the schema v1 field list. `pd.read_json` is not required in CI — pandas is not a project dependency and `json.loads` is strictly stricter for schema validation.

### Key Entities

- **AttemptRecord**: One structured log row per attempt event (start or end). Fields below. Two rows per attempt (start + end); joined by `attempt_id`.
- **AttemptLog**: The module responsible for writing AttemptRecord rows. Exposes `open_attempt(...)` and `close_attempt(...)`. Writes to `logs/attempts/YYYY-MM-DD.jsonl`. Silently swallows write errors after logging. Write-only — never reads back from the JSONL.
- **`PersistedSession.last_attempt_id`**: New field (this spec) storing the most recent `attempt_id` for a card. Written by `dispatch_card` after each `open_attempt` call; read on re-dispatch to populate `parent_attempt_id`. Survives daemon restarts via the existing `coordinare.state.json` checkpoint.

### AttemptRecord Schema (v1)

| Field | Type | Set at | Status | Description |
|---|---|---|---|---|
| `schema_version` | `int` | both | new | Always `1` for this spec; bump on future schema changes |
| `attempt_id` | `str` (UUID4) | start | new | Unique per attempt; UUID4 (2^128 possible values) |
| `task_id` | `str` | both | exists | GitHub card ID — already in `CoordinareState["current_card"]["id"]`; denormalized onto end rows so consumers can filter without a join |
| `parent_attempt_id` | `str \| null` | start | new | Previous attempt's `attempt_id` on a bounce, else null |
| `model_tier` | `str \| null` | start | new | `"cheap"` \| `"mid"` \| `"frontier"` \| null — tier taxonomy does not yet exist; inferred from model name or config |
| `model_name` | `str \| null` | start/end | partial | Model ID from config at dispatch; may need performer response to confirm |
| `routing_reason` | `str` | start | new | `"default_policy"` always until router lands; `"explore_random"` \| `"router_v1"` \| `"manual"` are future values |
| `started_at` | `str` (ISO UTC) | both | new | Timestamp at `dispatch_card`; denormalized onto end rows so `wall_time_s` is independently verifiable without fetching the start row |
| `ended_at` | `str \| null` (ISO UTC) | end | new | Timestamp at verdict; not currently persisted |
| `wall_time_s` | `float \| null` | end | new | Derived from `ended_at - started_at` |
| `tokens_in` | `int \| null` | end | partial | `card_tokens_total` exists but is a lifetime aggregate; per-attempt counts need performer response |
| `tokens_out` | `int \| null` | end | partial | Same as above |
| `verdict` | `str \| null` | end | partial | Mapped from `StageVerdict.verdict` (free-text str) using the table below. Unrecognized values map to `"error"` — never `"fail"` — so unknown values cannot masquerade as task difficulty. |
| `verdict_source` | `str \| null` | end | new | `"qa_role"` \| `"human"` \| `"grader"` — needs derivation from which node closed the attempt |
| `terminal_state` | `str \| null` | end | new | `"merged"` \| `"blocked"` \| `"abandoned"` \| null; derivable from which terminal node runs |
| `spec_word_count` | `int \| null` | start | new | Word count of card description at dispatch time |
| `spec_has_acceptance_tests` | `bool \| null` | start | new | Heuristic: presence of "given/when/then" or "acceptance" in card body |
| `repo_area` | `str \| null` | start | new | Top-level directory heuristic from card labels or config |
| `source` | `str` | both | new | `"live"` \| `"backfill"` — marks backfilled rows |

### Stage Marker → Taxonomy Mapping

The marker is the `status` string emitted by `monitor_performer.py` at the end of a performer turn (e.g. `"qa_passed"`, `"changes_requested"`). The implementer MUST map it to the AttemptRecord taxonomy using this table. Any value not in this table MUST map to `"error"`.

**Important:** `partial_progress` and `working` are non-terminal — the caller MUST NOT call `close_attempt` for these markers. They appear here only as a safety note; if they do reach `map_verdict` they fall through to `"error"`.

| Stage marker | AttemptRecord `verdict` | Reason |
|---|---|---|
| `"approved"` | `pass` | Review approved the work |
| `"qa_passed"` | `pass` | QA approved the work |
| `"security_passed"` | `pass` | Security review passed |
| `"docs_committed"` | `pass` | Documentation stage complete |
| `"changes_requested"` | `fail` | Reviewer requested changes; card will be re-dispatched |
| `"qa_failed"` | `fail` | QA rejected the work |
| `"security_failed"` | `fail` | Security review rejected the work |
| `"error"` | `error` | Infra failure |
| `"blocked"` | `error` | Performer blocked; not a content judgment |
| `"env_blocked"` | `error` | Environment blocked (spec 095); not task difficulty |
| `"qa_env_blocked"` | `error` | QA environment blocked; not task difficulty |
| `"session_expired"` | `timeout` | Performer session ran out |
| `"token_limit"` | `timeout` | Token budget exhausted |
| `"idle_timeout"` | `timeout` | Performer timed out waiting for output |
| anything else | `error` | Unknown value must never be treated as task difficulty |

### Deferred / Optional Fields

Fields excluded from the v1 main schema due to implementation cost or missing dependencies. Documented here so consumers know they are planned. Revisit when the relevant work lands.

| Field | Reason to defer | When to revisit |
|---|---|---|
| `assignment_propensity` | Always `null` until the shadow router (Workstream D) exists. Adds a null field to every row with no value today. | When Workstream D lands |
| `retries_internal` | Performers don't currently report internal retries. Requires protocol changes. Low value for routing/benchmarking — only the final verdict matters for those goals. | When performer protocol is extended |
| `spec_ref` | Requires heuristically parsing card body to find a spec path. Unreliable on cards that don't follow naming conventions. Best-effort at best. | When card→spec linking is more structured |
| `files_touched` | Requires parsing the PR diff at attempt-end. Non-trivial work. Outcome-side field — excluded from router training anyway. | When diff analytics become a priority |
| `loc_added` | Same as above. | Same as above |
| `loc_removed` | Same as above. | Same as above |
| `diff_uri` | Requires knowing where diff artifacts are stored. No artifact storage is defined yet. | When artifact storage is defined |

---

## Success Criteria *(mandatory)*

- **SC-001** *(real-world bar, not a CI test)*: One ordinary day of coordinare operation yields a JSONL file where every line parses cleanly with `json.loads` and conforms to schema v1. Verified manually after first deployment.
- **SC-002**: Golden test using a controlled fake card verifies: (a) a start row exists with `attempt_id`, `task_id`, `started_at`, `routing_reason`, and all `spec_*` fields populated; (b) an end row exists with the same `attempt_id` and `verdict`, `verdict_source`, `ended_at`, `wall_time_s` populated; (c) every row parses cleanly with `json.loads` and contains all required schema v1 fields.
- **SC-003**: A card that bounces twice then merges produces exactly 3 attempt IDs sharing the same `task_id` (6 rows total — one start row and one end row per attempt); 2 end rows have `verdict: "fail"`, 1 end row has `verdict: "pass"` and `terminal_state: "merged"`.
- **SC-004**: A simulated JSONL write failure (mocked `open` raising `OSError`) does not raise an exception in the calling graph node and does not affect card processing.
- **SC-005**: `logs/attempts/` is absent from `git status` output after the directory is created (confirmed by `.gitignore` entry).
- **SC-006**: A simulated system error (Docker failure) produces an end row with `verdict: "error"`, not `verdict: "fail"`. A simulated timeout produces `verdict: "timeout"`. Neither is ever written as `"fail"`.
- **SC-007**: If a card body is modified after dispatch, the AttemptRecord's `spec_*` fields reflect the body at dispatch time, not the modified body — verified by changing card state between `open_attempt` and `close_attempt` in a controlled test.
- **SC-008**: A JSONL file containing a start row with no matching end row (simulating a mid-attempt daemon restart) parses cleanly with `json.loads`; the incomplete row has `ended_at: null` and consumers handle it without crashing.

---

## Assumptions

- **A-001**: `dispatch_card` has access to the card's description, GitHub card ID, and configured model at the time of dispatch. If model tier is not yet resolvable, `model_tier` and `model_name` are written as `null` and updated at attempt-end.
- **A-002**: Coordinare runs as a single process on a single host. Append-only JSONL is sufficient; no database or queue is needed until a second consumer requires queries across multiple hosts. This spec does require one state change: a new `last_attempt_id` field on `PersistedSession` (see FR-008) to survive restarts between dispatches. No other persisted state changes are needed.
- **A-003**: The `logs/attempts/` directory is created by `AttemptLog` on first write if it does not exist.
- **A-004**: Token counts come from the performer's response contract (already present in the HTTP performer protocol). If a performer does not report tokens, `tokens_in` and `tokens_out` are `null`.
- **A-005**: `spec_has_acceptance_tests` is a heuristic — presence of the word "acceptance" or "given/when/then" in the card body. Exact implementation is left to the engineer; the field is best-effort.
- **A-006**: Grafana wiring and historical backfill are follow-on work dependent on this spec landing first and are out of scope here.
- **A-007**: The `bounces_total` Prometheus counter (spec 009 extension, feat/per-role-quality-metrics) remains in place as a real-time aggregate signal. AttemptRecords are the historical source of truth; the counter is derived convenience. They are complementary, not redundant.

- **A-008**: If the daemon restarts between `open_attempt` and `close_attempt`, `AttemptLog._log_paths` is lost. The log path for the in-flight attempt MUST be persisted on `PersistedSession` alongside `last_attempt_id` so it survives restarts. On recovery, `close_attempt` reads the stored path from `PersistedSession` rather than falling back to today's date. Implementation is deferred to T-A2 when `AttemptLog` is wired into the graph nodes.
