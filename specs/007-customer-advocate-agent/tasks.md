# Tasks: Customer Advocate Agent

**Input**: Design documents from `/specs/007-customer-advocate-agent/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

**Tests**: Included — Constitution Principle II (Testing Discipline) is non-negotiable; all new public interfaces require unit tests, and each user story requires an integration test.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story increment.

**Remediations applied**: C1 (T010 — FR-001a board-workflow skip), H1 (plan.md source tree), H2 (T015 — asyncio concurrency), H3 (spec FR-010 fixed separately), M1 (T012 — disclosure assertion), M2 (T032 — provider score logging).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US4)
- Exact file paths included in every description

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create new module placeholders and extend test scaffolding

- [X] T001 Create new source files with empty module stubs: `src/coordinare/models/advocate.py`, `src/coordinare/services/advocate.py`, `src/coordinare/services/scoring.py`, `src/coordinare/graph/nodes/advocate.py`
- [X] T002 [P] Create new test file stubs: `tests/unit/services/test_scoring.py`, `tests/unit/graph/nodes/test_advocate.py`, `tests/unit/models/test_advocate_models.py`, `tests/integration/test_advocate_scan.py`, `tests/contract/test_github_advocate_queries.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 [P] Add advocate model enums (IssueType, AdvocateAction, EscalationReason) and all entity dataclasses (IssueClassification, AdvocateResponse, EscalationRecord, DocumentationSource, ScoringProvider, ConsensusScore) to `src/coordinare/models/advocate.py` per `specs/007-customer-advocate-agent/data-model.md`
- [X] T004 [P] Add `AdvocateConfig` Pydantic model (all fields per data-model.md Config section, including `github_repo: str` and `doc_branch: str = "HEAD"`) and `advocate: AdvocateConfig = Field(default_factory=AdvocateConfig)` to `ProjectConfiguration` in `src/coordinare/config.py`; add validator: when `enabled=True`, `github_repo` must be non-empty; `doc_branch` is the git ref used when fetching documentation files (defaults to `"HEAD"` for the repo's default branch)
- [X] T005 Add `AdvocateServiceProtocol` (with `async def scan_and_respond(processed_ids: set[str]) -> set[str]` method) and extend `CoordinareState` with `advocate_service: AdvocateServiceProtocol | None` and `advocate_history: set[str]` fields in `src/coordinare/graph/state.py`; update `initial_state()` to initialise `advocate_history` as empty set
- [X] T006 Add six new methods to `GitHubService` in `src/coordinare/services/github.py`: `get_repository_id(owner, repo)`, `get_label_ids(owner, repo)`, `ensure_labels_exist(owner, repo, handled_label, escalation_label)`, `add_labels(issue_id, label_ids)`, `list_open_issues(owner, repo, first=20)`, `get_file_content(owner, repo, path, ref)` — with corresponding GraphQL strings per `specs/007-customer-advocate-agent/contracts/github-graphql-advocate.md`
- [X] T007 Add `advocate_scan` placeholder node to `_DEFAULT_NODES` dict, register it in `CoordinareGraphBuilder.build()`, and reroute graph entry: `START → advocate_scan → check_board` (replacing direct `START → check_board` edge) in `src/coordinare/graph/builder.py`
- [X] T008 [P] Add three advocate Prometheus metrics to `src/coordinare/metrics.py`: `coordinare_advocate_issues_processed_total` (Counter, label: `action`; incremented for ALL actions including `action="escalated"` inside `_do_escalate` so dashboards can sum across all action types), `coordinare_advocate_issues_escalated_total` (Counter, label: `reason`; incremented alongside processed_total on each escalation for reason-level breakdown), `coordinare_advocate_scan_duration_seconds` (Histogram) per `specs/007-customer-advocate-agent/quickstart.md` observability table
- [X] T009 Add advocate bootstrap to `src/coordinare/__main__.py` (in `_bootstrap_services`): (a) instantiate `AdvocateService` and inject into initial state as `advocate_service` when `config.advocate.enabled`, (b) call `github_service.ensure_labels_exist()` during startup when advocate is enabled, (c) pass `None` for `advocate_service` and skip label setup when disabled (note: bootstrap lives in `__main__.py`, not `daemon.py`)
- [X] T010 [P] Exclude issues whose GitHub labels include `config.advocate.handled_label` or `config.advocate.escalation_label` from the `TODO` pool (satisfying FR-001a); implemented by filtering `item_labels` in `src/coordinare/graph/nodes/check_board.py` (note: `route_from_board_check` in `routing.py` only reads `state["phase"]` so cannot filter here; label data added to `poll_board()` return and consumed in `check_board.py`); unit tests in `tests/unit/test_routing.py`

**Checkpoint**: Foundation ready — user story implementation can begin

---

## Phase 3: User Story 1 — Auto-Reply to Customer Questions (Priority: P1) 🎯 MVP

**Goal**: Every new question/confusion issue receives an accurate, documented, non-duplicate advocate reply within one poll cycle.

**Independent Test**: Create a GitHub issue asking about a documented topic. Run `--max-cycles 1`. Verify: (1) `advocate-handled` label applied, (2) a comment cites the source document, (3) comment contains the AI-disclosure line, (4) no second comment on the next cycle.

### Tests for User Story 1

> **Write these first — verify they FAIL before implementation**

- [X] T011 [P] [US1] Write unit tests for `ClaudeScorer` in `tests/unit/services/test_scoring.py`: (a) valid JSON response → correct classification + confidence returned, (b) JSON parse failure → `ScoringResult` with `confidence=0.0` and `reasoning="parse_error"`, (c) API exception → `ScoringResult` with `confidence=0.0` and `reasoning="api_error"`, (d) confidence clamped to `[0.0, 1.0]` if model returns out-of-range value
- [X] T012 [P] [US1] Write unit tests for advocate node question/confusion path in `tests/unit/graph/nodes/test_advocate.py`: (a) new question issue → label applied first, then reply posted; (b) posted comment contains `config.disclosure_template` text (FR-014); (c) duplicate issue already in `processed_ids` → skipped, no comment; (d) issue already carrying `advocate-handled` label → skipped; (e) `advocate_service=None` → node returns state unchanged

### Implementation for User Story 1

- [X] T013 [US1] Implement `ScoringResult` dataclass and `ScoringProviderProtocol` in `src/coordinare/services/scoring.py` per `specs/007-customer-advocate-agent/contracts/scoring-provider.md`
- [X] T014 [US1] Implement `ClaudeScorer` in `src/coordinare/services/scoring.py`: V1 single Claude API call (classify + score + response_text), structured JSON prompt per `contracts/scoring-provider.md`, JSON parse failure and API errors return `ScoringResult(confidence=0.0)` without raising (depends on T013)
- [X] T015 [US1] Implement `AdvocateService` class in `src/coordinare/services/advocate.py` with: `__init__` (inject GitHubService, notification services, config, scorers list), `scan_and_respond(processed_ids)` for question/confusion path — call `list_open_issues`, filter labeled/processed, process up to 20 issues using `asyncio.gather` with `asyncio.Semaphore(5)` per `specs/007-customer-advocate-agent/research.md` R9; per-issue: apply `advocate-handled` label first, call ClaudeScorer, post reply with citation + disclosure appended, add to processed_ids; wrap GitHub API calls in try/except — on failure log structured error and continue to next issue (depends on T005, T006, T014)
- [X] T016 [US1] Implement `advocate_scan` LangGraph node function in `src/coordinare/graph/nodes/advocate.py`: check `state["advocate_service"]` is not None (return state unchanged if disabled), call `await advocate_service.scan_and_respond(state["advocate_history"])`, update `state["advocate_history"]` with returned set, emit `advocate_scan_complete` structured log (depends on T005, T015)
- [X] T017 [US1] Integration test for full question→auto-reply cycle in `tests/integration/test_advocate_scan.py`: mocked `GitHubService` and `ClaudeService`; verify: issue fetched → label applied before comment → comment text contains citation and disclosure → `processed_ids` updated; second cycle on same issue → no duplicate comment

**Checkpoint**: US1 fully functional and independently testable

---

## Phase 4: User Story 2 — Human Escalation for Low-Confidence or Sensitive Issues (Priority: P2)

**Goal**: Issues with sensitive keywords, low confidence, or complaint classification are escalated to human reviewers — never auto-posted.

**Independent Test**: Create a "billing" issue. Run `--max-cycles 1`. Verify: (1) `needs-human` label applied, (2) holding comment posted, (3) Slack/email notification sent, (4) no AI answer posted. Separately create a question issue and score it at 0.40 confidence (mock); verify same escalation outcome.

### Tests for User Story 2

> **Write these first — verify they FAIL before implementation**

- [X] T018 [P] [US2] Write unit tests for all three escalation triggers in `tests/unit/graph/nodes/test_advocate.py`: (a) sensitive keyword in issue title/body → escalation short-circuits before Claude call, `needs-human` label applied, holding comment posted, no AI answer; (b) `complaint` classification → escalation regardless of confidence score; (c) confidence below threshold → escalates question/confusion; (d) `EscalationRecord` logged with correct `reason` for each trigger

### Implementation for User Story 2

- [X] T019 [US2] Add sensitive keyword matching to `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`: case-insensitive substring match of `config.sensitive_keywords` against issue title + body, evaluated before the Claude API call; on match: apply `escalation_label` first, post holding comment, dispatch Slack/email notification, record `EscalationRecord(reason=sensitive_keyword)`
- [X] T020 [US2] Add complaint-classification and low-confidence escalation paths to `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`; reuse same escalation action (label + holding comment + notify) for all three trigger types; record `EscalationRecord` with correct `reason` (`complaint` or `low_confidence`)
- [X] T021 [US2] Integration test for escalation cycle in `tests/integration/test_advocate_scan.py`: (a) sensitive keyword issue → `needs-human` label + holding comment + notification sent + no AI answer; (b) low-confidence mock → same outcome; (c) notification payload contains issue URL and escalation reason

**Checkpoint**: US1 + US2 both independently testable

---

## Phase 5: User Story 3 — Documentation-Sourced Answers (Priority: P3)

**Goal**: Every auto-reply is grounded in configured documentation files; undocumented topics escalate with `no_documentation_match`.

**Independent Test**: Configure `doc_sources: ["README.md"]`. Mock `get_file_content` returning README text → verify citation appears. Mock issue about undocumented topic → verify escalation with `reason: no_documentation_match`. Mock all sources returning null → verify all issues escalate with `reason: no_documentation_configured`.

### Tests for User Story 3

> **Write these first — verify they FAIL before implementation**

- [X] T022 [P] [US3] Write unit tests for doc loading in `tests/unit/graph/nodes/test_advocate.py`: (a) reachable doc → text content passed to scorer; (b) `get_file_content` returns null → `DocumentationSource.reachable=False`; (c) all sources unreachable → every question/confusion issue in cycle escalates with `no_documentation_configured`; (d) scorer returns `response_text=None` (topic not covered) → escalates with `no_documentation_match`

### Implementation for User Story 3

- [X] T023 [US3] Implement `_load_documentation_sources()` in `src/coordinare/services/advocate.py`: call `GitHubService.get_file_content()` for each path in `config.doc_sources`, build list of `DocumentationSource` objects; emit `advocate_doc_fetch_warning` structured log for each unreachable file (depends on T006)
- [X] T024 [US3] Integrate doc loading into `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`: call `_load_documentation_sources()` before processing issues; if all sources unreachable → escalate all question/confusion issues with `no_documentation_configured`; pass concatenated reachable doc content to `ClaudeScorer.score()`; if scorer returns `response_text=None` → escalate with `no_documentation_match` (depends on T023)

**Checkpoint**: US1 + US2 + US3 all independently testable; answers are always doc-grounded

---

## Phase 6: User Story 4 — Feature-Request and Off-Topic Triage (Priority: P4)

**Goal**: Feature requests, bug reports, and off-topic issues are triaged correctly: acknowledged, silently triaged, or redirected.

**Independent Test**: Create a feature-request issue → verify acknowledgement comment and `advocate-handled` label. Create a bug-report issue → verify `advocate-handled` label, no comment, board workflow still picks it up. Create an off-topic issue → verify redirect comment with `support_channel_url` and `advocate-handled` label.

### Tests for User Story 4

> **Write these first — verify they FAIL before implementation**

- [X] T025 [P] [US4] Write unit tests for triage paths in `tests/unit/graph/nodes/test_advocate.py`: (a) `feature_request` → `advocate-handled` label applied first, then acknowledgement template posted, `AdvocateResponse(action=acknowledged)` recorded; (b) `bug_report` → `advocate-handled` label applied, no comment posted, `AdvocateResponse(action=triaged)` recorded; (c) `off_topic` → `advocate-handled` label applied, redirect template posted with `support_channel_url` substituted, `AdvocateResponse(action=redirected)` recorded

### Implementation for User Story 4

- [X] T026 [US4] Add `feature_request` acknowledgement path to `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`: apply `handled_label` first, post `config.acknowledgement_template`, record `AdvocateResponse(action=acknowledged)`
- [X] T027 [US4] Add `bug_report` triage path to `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`: apply `handled_label` only, post NO comment, record `AdvocateResponse(action=triaged)`; note: `advocate-handled` label ensures FR-001 dedup works; board workflow still processes the issue via project board column (not label-based)
- [X] T028 [US4] Add `off_topic` redirect path to `AdvocateService.scan_and_respond()` in `src/coordinare/services/advocate.py`: apply `handled_label` first, post `config.redirect_template` with `{support_channel_url}` substituted, record `AdvocateResponse(action=redirected)`

**Checkpoint**: All four user stories independently functional

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Contract tests, model validation, structured logging, lint gate

- [X] T029 [P] Add contract tests for all six GitHub advocate GraphQL queries/mutations in `tests/contract/test_github_advocate_queries.py` per `specs/007-customer-advocate-agent/contracts/github-graphql-advocate.md`: validate query strings parse via `gql()` without error; validate expected response field structure for each query
- [X] T030 [P] Add unit tests for `AdvocateConfig` validation in `tests/unit/test_config.py`: `enabled=True` with empty `github_repo` → raises `ValueError`; `confidence_threshold` outside `(0.0, 1.0]` → raises; default `sensitive_keywords` list matches FR-007 enumeration
- [X] T031 [P] Add unit tests for advocate model field validation in `tests/unit/models/test_advocate_models.py`: `confidence_score` outside `[0.0, 1.0]` raises; `IssueType` with invalid string raises; `ConsensusScore.final_score` equals mean of `provider_scores`
- [X] T032 Add structured `structlog` logging to all advocate events in `src/coordinare/services/advocate.py` and `src/coordinare/graph/nodes/advocate.py`: `advocate_scan_start` (cycle_id, open_issues_fetched, unprocessed_count), `advocate_issue_processed` (issue_id, issue_number, classification, action, confidence, **provider_scores**, elapsed_ms — satisfying FR-005 log requirement), `advocate_issue_escalated` (issue_id, issue_number, reason, notified_channel), `advocate_scan_complete` (cycle_id, issues_processed, elapsed_ms), `advocate_doc_fetch_warning` (file_path, ref, error)
- [X] T033 Verify `ruff check src/ tests/` and `mypy src/` pass with zero errors for all new and modified advocate files; fix any lint or type errors

---

## Phase 8: Post-Analyze Remediations (speckit.analyze findings)

**Purpose**: Address gaps identified by speckit.analyze after initial implementation; all tasks below are complete.

- [X] T034 Add `doc_branch: str = "HEAD"` to `AdvocateConfig` in `src/coordinare/config.py`; wire into `_load_documentation_sources()` replacing hardcoded `"main"` ref; update `get_file_content()` default ref from `"main"` to `"HEAD"` in `src/coordinare/services/github.py`; add tests `test_advocate_doc_branch_defaults_to_head` and `test_advocate_doc_branch_configurable` in `tests/unit/test_config.py` (resolves spec gap U1)
- [X] T035 Add SC-006 performance throughput test in `tests/perf/test_advocate_throughput.py`: mock 20 issues with instant-returning scorers, assert total elapsed < 10s; satisfies Constitution Principle IV automated benchmark requirement (resolves spec gap C1)
- [X] T036 Fix `compute_consensus` in `src/coordinare/services/scoring.py` to exclude providers that returned a failed result (classification=None) from score aggregation — use `successful` list instead of `valid` so parse/api errors don't suppress the mean; add `TypeError` to the parse-error exception handler so malformed-but-valid-JSON fields (e.g. null confidence) are caught correctly
- [X] T037 Add 7 unit tests covering previously uncovered paths: `advocate_scan` node happy path and exception path; `list_open_issues` failure; per-issue exception skips issue but continues others; all-scorers-failed escalation; `add_labels` GitHub failure; `add_comment` GitHub failure — raises coverage from 89% to 90.1%; update `pyproject.toml` `fail_under` from 80 → 90
- [X] T038 Address all Copilot PR review comments: `provider_scores` field added to all `advocate_issue_processed` structured log events (FR-005); `METRICS.advocate_issues_processed_total.labels(action="escalated").inc()` added to `_do_escalate`; correct doc comment in `config.py` to reference GitHub GraphQL API; align `get_file_content` default ref with `doc_branch` default

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
  - T003, T004, T008 can run in parallel once T001 is complete
  - T005 + T006 + T010 can run in parallel (different files)
  - T007 depends on T005 (state extension)
  - T009 depends on T004 (config) and T006 (GitHub service)
- **Phase 3 (US1)**: Depends on Phase 2 complete — T011/T012 tests can start as soon as T003 (models) is done; T013/T014 require T003; T015 requires T014; T016 requires T015
- **Phase 4 (US2)**: Depends on Phase 3 complete (US2 extends AdvocateService from US1)
- **Phase 5 (US3)**: Depends on Phase 3 complete (US3 extends AdvocateService from US1); can run in parallel with Phase 4
- **Phase 6 (US4)**: Depends on Phase 3 complete; can run in parallel with Phases 4 and 5
- **Phase 7 (Polish)**: Depends on Phases 4–6 complete

### User Story Dependencies

- **US1 (P1)**: Only depends on Foundational (Phase 2)
- **US2 (P2)**: Extends AdvocateService from US1; must follow Phase 3
- **US3 (P3)**: Extends AdvocateService from US1; can be developed in parallel with US2 (different methods)
- **US4 (P4)**: Extends AdvocateService from US1; can be developed in parallel with US2 and US3

### Within Each User Story

1. Tests written and verified to FAIL first (Constitution II)
2. Models/protocols before services
3. Services before node function
4. Node function before integration test
5. Story complete before moving to next priority

### Parallel Opportunities

- T003, T004, T008 (Phase 2) — fully independent files
- T005, T006, T010 (Phase 2) — fully independent files
- T011, T012 (Phase 3 tests) — can run in parallel once T003 done
- T013 must precede T014; T014 must precede T015; T015 must precede T016 (sequential chain)
- After Phase 3: US2 (T018–T021), US3 (T022–T024), US4 (T025–T028) can all proceed in parallel
- Phase 7 tasks T029, T030, T031 are fully parallel

---

## Parallel Example: User Story 1

```bash
# Step 1 — write tests in parallel (after T003 models exist):
Task: "T011 — unit tests for ClaudeScorer in tests/unit/services/test_scoring.py"
Task: "T012 — unit tests for advocate node (incl. disclosure assertion) in tests/unit/graph/nodes/test_advocate.py"

# Step 2 — implement protocol then scorer sequentially:
Task: "T013 — ScoringResult + ScoringProviderProtocol in src/coordinare/services/scoring.py"
Task: "T014 — ClaudeScorer in src/coordinare/services/scoring.py" (after T013)

# Step 3 — sequential service → node → integration:
Task: "T015 — AdvocateService with asyncio.Semaphore(5) in src/coordinare/services/advocate.py" (after T014)
Task: "T016 — advocate_scan node in src/coordinare/graph/nodes/advocate.py" (after T015)
Task: "T017 — integration test in tests/integration/test_advocate_scan.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (auto-reply to questions)
4. **STOP and VALIDATE**: Run US1 independent test and integration suite
5. Deploy/demo advocate with question-answering only

### Incremental Delivery

1. **Setup + Foundational** → graph structure extended, GitHub service ready, board isolation safe
2. **US1 complete** → coordinare answers documented questions automatically (MVP)
3. **US2 complete** → unsafe issues route to humans; no accidental AI responses on sensitive topics
4. **US3 complete** → all answers are documentation-grounded; hallucination-free
5. **US4 complete** → full triage coverage (feature requests, bugs, off-topic)
6. Each story adds value without breaking previous stories

### Parallel Team Strategy

With multiple developers, after Phase 2 completes:
- Developer A: US2 (escalation) — extends `AdvocateService` escalation methods
- Developer B: US3 (doc sourcing) — extends `AdvocateService` doc-loading methods
- Developer C: US4 (triage) — extends `AdvocateService` triage action methods
- All three modify `AdvocateService` in the same file — coordinate to avoid merge conflicts

---

## Summary

| Phase | Tasks | Parallel | User Story |
|-------|-------|----------|------------|
| Phase 1: Setup | T001–T002 | T002 [P] | — |
| Phase 2: Foundational | T003–T010 | T003, T004, T008, T010 [P] | — |
| Phase 3: US1 (MVP) | T011–T017 | T011, T012 [P] | US1 |
| Phase 4: US2 | T018–T021 | T018 [P] | US2 |
| Phase 5: US3 | T022–T024 | T022 [P] | US3 |
| Phase 6: US4 | T025–T028 | T025 [P] | US4 |
| Phase 7: Polish | T029–T033 | T029, T030, T031 [P] | — |
| **Total** | **33 tasks** | **10 [P]** | — |

---

## Notes

- [P] tasks operate on different files or are data-independent; safe to run concurrently
- [Story] label maps each task to a specific user story for traceability
- Each user story is independently completable and testable after the Foundational phase
- The advocate feature is entirely disabled (zero cost) when `advocate.enabled: false` in config
- Commit after each task or logical group; use conventional commits (`feat:`, `test:`, `fix:`)
- Stop at the Phase 3 checkpoint to validate US1 independently before continuing
