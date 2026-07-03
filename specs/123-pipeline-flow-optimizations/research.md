# Research: 123-pipeline-flow-optimizations

## US1 — Tech_writer doc-change gate

### Decision: Gate at `dispatch_performer.py` before stage dispatch, not after

**Rationale**: `dispatch_performer.py:51-57` defines `_PR_REQUIRED_STAGES` which includes "documenting". The doc gate must fire before `_dispatch_stage()` picks the role. The PR diff is already available at dispatch time (injected at lines ~1043-1050). Checking whether the diff touches any `docs/` path is a simple `any(p.startswith("docs/") for p in changed_files)` against the already-fetched diff metadata — zero extra I/O.

**What `doc_dedup.py` actually does**: `find_duplicate_sections(workspace_path)` is read-only analysis of headings overlap; `merge_duplicate_sections()` mutates files. Neither API provides a content-hash "did this commit change docs" check. FR-003's content-hash dedup is **deferred** — `doc_dedup.py` solves heading dedup within docs files, not "did the PR touch docs" detection. The primary gate (FR-001/002) is sufficient.

**Alternatives considered**:
- Post-dispatch suppression: would still spend the slot/token budget before detecting a no-op → rejected
- Content-hash comparison of last committed docs: would require reading all docs blobs from GitHub API — expensive, deferred to FR-003

---

## US2 — QA persona scope

### Decision: Remove code-quality/linting instructions from QA persona text only

**Rationale**: `persona_service.py` QA persona (~lines 425-522) includes `executed_checks` with exit codes and verification steps that overlap with the reviewer. The persona text is the only surface to change — no graph-level wiring, no new contracts. The `_build_system_prompt()` pattern for QA builds a string; editing the string is sufficient.

**What to remove**: Instructions to run linters, check test coverage percentages, or report code style issues. Keep: acceptance criteria verification, visual evidence capture (screenshot instructions added in prior session), `qa_passed`/`changes_requested` JSON contract.

**Alternatives considered**:
- Graph-level role-separation enforcement: overkill — persona text already drives the model's behavior
- New QA-specific tool calls: not warranted, model follows prompt instructions

---

## US3 — Split bounce budget

### Decision: Two new PersistedSession fields; legacy migration on read

**Rationale**: `feedback_cycle_count` lives on the graph state, incremented in `monitor_performer.py` around lines 342-425. `PersistedSession` in `graph/state.py` already has `feedback_cycle_count: int` and `total_feedback_cycles: int`. The split adds two new fields:
- `content_feedback_cycles: int` (default 0): incremented on `changes_requested`
- `transient_error_cycles: int` (default 0): incremented on `env_blocked`, `system_error`, `unknown` backend failures

Legacy migration: on `PersistedSession` construction/load, if `feedback_cycle_count > 0` and `content_feedback_cycles == 0`, set `content_feedback_cycles = feedback_cycle_count`. This is a read-time migration — no `coordinare.state.json` mutation required.

`_feedback_cycle_exhausted()` currently reads `feedback_cycle_count` and compares to `config.max_feedback_cycles`. The new logic branches on failure type before incrementing the appropriate counter and checking its limit.

**Alternatives considered**:
- Single counter with separate "reason" flag: doesn't give independent budgets, same exhaustion problem
- Separate `env_blocked` exhaustion already in spec-095 (`ENV_BLOCKED` hold): `transient_error_cycles` is the per-card accumulator that feeds the spec-095 path when exhausted

---

## US4 — Assessor Q&A carryover

### Decision: Persist `open_questions` on PersistedSession; inject as `prior_clarifications`

**Rationale**: `PersistedSession` has `clarifications_count_at_dispatch: int` (a snapshot counter) but not the actual Q&A text. The assessor's output (performer report JSON) includes `open_questions`. This field needs to be extracted from the assessor result in `monitor_performer.py` after a successful assessor run and persisted on the session. On re-dispatch, `dispatch_performer.py` injects it into the card context payload.

New field: `open_questions: list[dict] = []` on `PersistedSession`. Each entry is `{"question": str, "answer": str}` as extracted from the performer's assessor result.

**Alternatives considered**:
- Store as raw string blob: harder to selectively inject subsets; structured list is cleaner
- Store in coordinare state's top-level dict: would couple to card_id key lookup — per-session field is simpler

---

## US5 — Multi-concern assessor gate

### Decision: Classify concern categories from reviewer feedback using existing comment classifier; route on 2+ distinct categories

**Rationale**: `check_board.py` already classifies review comments via `_classify_with_ai()`. The result includes concern categories. After reviewer returns `changes_requested`, the coordinare evaluates next stage. Adding a check: if `len(set(concern_categories)) >= 2`, route to `assessing` instead of `implementing`.

Concern categories to classify: `architecture`, `implementation`, `style`, `tests`. A comment with no classifiable category → treated as `implementation` (safe default).

**Alternatives considered**:
- Always route through assessor on `changes_requested`: too many assessor round-trips for simple style fixes
- Only route on explicit "architecture" category: not broad enough — architecture+implementation is a common multi-concern case

---

## US6 — Comment dedup before classification

### Decision: Filter `processed_issue_comment_ids` before calling `_classify_with_ai()`

**Rationale**: `check_board.py` lines 213-215 already tracks `processed_issue_comment_ids: set[int]`. The dedup filter currently runs after classification to drop already-processed results. Moving the filter to before the `_classify_with_ai()` call is a 3-line change: filter the comment list first, skip the AI call entirely if the filtered list is empty.

**Alternatives considered**:
- Cache classification results: heavier — dedup before the call achieves the same result with no cache management

---

## US7 — Closer persona scope

### Decision: Rewrite closing_review persona to thread-resolution-only

**Rationale**: `persona_service.py` closer persona (~lines 524-574) currently includes CI linter verification. The closer's job is to confirm that open reviewer comment threads are resolved and CI is green — not to redo code review. Persona text change only; no graph changes.

**Alternatives considered**:
- Keep linting in closer as a safety net: reviewer already confirmed quality; double-checking wastes a full performer dispatch

---

## Files Changed Summary

| File | Change type |
|------|-------------|
| `src/coordinare/graph/nodes/dispatch_performer.py` | Gate: skip documenting if no `docs/` changes; inject `prior_clarifications` for assessor re-dispatch; route `changes_requested` through assessor if 2+ concern categories |
| `src/coordinare/graph/nodes/monitor_performer.py` | Split `feedback_cycle_count` → `content_feedback_cycles` / `transient_error_cycles`; persist `open_questions` from assessor result |
| `src/coordinare/graph/state.py` | Add `content_feedback_cycles`, `transient_error_cycles`, `open_questions` to `PersistedSession`; legacy migration on load |
| `src/coordinare/graph/nodes/check_board.py` | Move `processed_issue_comment_ids` dedup filter before `_classify_with_ai()` call |
| `src/coordinare/services/persona_service.py` | Remove code-quality/linting from QA persona; rewrite closer persona to thread-resolution-only |
| `tests/unit/graph/nodes/test_dispatch_performer.py` | New/extended tests for doc gate, prior_clarifications injection, multi-concern routing |
| `tests/unit/graph/nodes/test_monitor_performer.py` | New/extended tests for split budget counters, open_questions persistence |
| `tests/unit/graph/nodes/test_check_board.py` | New/extended tests for pre-classification dedup |
| `tests/unit/graph/state/test_persisted_session.py` | Legacy migration test for `feedback_cycle_count` → `content_feedback_cycles` |
