# Tasks: Security Performer

**Input**: Design documents from `/specs/022-security-performer/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup (Shared Infrastructure)

- [X] T001 Add `SECURITY_MAX_CYCLES: int = 3` to `agent/performer/src/performer/config.py`
- [X] T002 Add `"security_passed"` and `"security_failed"` to `PerformanceState` in `agent/performer/src/performer/models.py`; add `security_findings: list[dict] = field(default_factory=list)` and `security_cycle: int = 0` fields to the `Performance` dataclass
- [X] T003 Add `"security_passed"` and `"security_failed"` to `PerformerStatusType` in `agent/performer/src/performer/protocol.py`; add `findings: list[dict] = Field(default_factory=list)` to `PerformerResponse`
- [X] T004 Add `"security_passed"` and `"security_failed"` to `StatusType` in `src/coordinare/protocol.py`; add `findings: list[dict]` to `ProtocolResponse`
- [X] T005 Update contract test in `tests/contract/test_agent_protocol.py`: add `security_passed` and `security_failed` to expected status values
- [X] T006 Write unit tests in `agent/performer/tests/unit/test_models.py` (extend): verify new states and fields

---

## Phase 2: Foundational (Blocking Prerequisites)

- [X] T007 Implement `post_pr_comment(owner, repo, pr_number, body, token)` async function in `agent/performer/src/performer/github.py`: POST to `/repos/{owner}/{repo}/issues/{pr_number}/comments`; raise `GitHubAPIError` on non-2xx
- [X] T008 Write unit tests for `post_pr_comment` in `agent/performer/tests/unit/test_github.py`

---

## Phase 3: User Story 1 — Pass a Clean Feature Branch (Priority: P1) 🎯 MVP

**Goal**: Security performer returns `security_passed` when no critical/high findings exist.

### Implementation for User Story 1

- [X] T009 [US1] Modify `agent/performer/src/performer/main.py`: add security branch gated on `perf.role == "security"` in `handle_status` when backend returns `done`; parse `backend_status.output` as JSON with `findings` list; if no critical/high findings, set `perf.state = "security_passed"` and return `PerformerResponse(status="security_passed")`
- [X] T010 [US1] Add `security_passed` and `security_failed` to terminal state guards and message loop break conditions in `agent/performer/src/performer/main.py`
- [X] T011 [US1] Verify `security_passed` is in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py` (added in 019)
- [X] T012 [P] [US1] Write unit tests: security role with no blocking findings → `security_passed`; implementer role unaffected

---

## Phase 4: User Story 2 — Block on Critical/High Findings (Priority: P1)

**Goal**: Security performer returns `security_failed` with findings routed to implementer or architect.

### Implementation for User Story 2

- [X] T013 [US2] In the security branch of `handle_status`: if critical/high findings exist, increment `security_cycle`; if `security_cycle >= SECURITY_MAX_CYCLES` return `blocked`; otherwise set `perf.state = "security_failed"` and return `PerformerResponse(status="security_failed", findings=[blocking findings])`
- [X] T014 [US2] Add `security_failed` handling to `src/coordinare/graph/nodes/monitor_performer.py`: extract findings from status, partition by `routing` field, store as `relay_feedback`, reset `performer_stage` to earliest routed target (implementer or architect), set `phase = "dispatching"`
- [X] T015 [P] [US2] Write unit tests in performer: security_failed with findings; max cycle → blocked; findings include routing field
- [X] T016 [P] [US2] Write unit tests in `tests/unit/graph/nodes/test_monitor_performer.py`: `security_failed` routes findings to implementer; architecture findings route to architect

---

## Phase 5: User Story 3 — Advisory Comments for Medium/Low (Priority: P2)

**Goal**: Medium/low findings posted as advisory PR comments without blocking.

### Implementation for User Story 3

- [X] T017 [US3] In the security branch of `handle_status`: before checking blocking findings, post advisory comments for medium/low findings via `post_pr_comment` with `[Advisory - Security]` label
- [X] T018 [P] [US3] Write unit test: medium-only findings → `security_passed` + advisory comments posted; mixed findings → advisory posted alongside `security_failed`

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T019 Run full test suite and linter — zero warnings, all pass
- [X] T020 Verify backward compatibility: implementer, architect, reviewer roles unaffected
- [X] T021 Verify `security_passed` in `TERMINAL_SUCCESS_STATES` in coordinare

---

## Dependencies & Execution Order

- **Setup (Phase 1)**: Start immediately
- **Foundational (Phase 2)**: Depends on Phase 1
- **US1 (Phase 3)**: Depends on Phase 2 — MVP
- **US2 (Phase 4)**: Depends on US1
- **US3 (Phase 5)**: Depends on Phase 2 — can parallel with US1/US2
- **Polish (Phase 6)**: Depends on all

## Implementation Strategy

Same pattern as 021 (reviewer): config + models + protocol → GitHub helper → performer logic → coordinare routing → tests. `security_passed` already in 019's TERMINAL_SUCCESS_STATES. `security_failed` is a new non-terminal status requiring coordinare-side handling (same pattern as `changes_requested` in 021).
