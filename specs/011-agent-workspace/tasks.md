# Tasks: Agent Workspace Management (011)

**Input**: Design documents from `/specs/011-agent-workspace/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story label (US1, US2, US3)
- No setup phase — this feature adds to an existing project

---

## Phase 1: Foundational (Blocking Prerequisites)

**Purpose**: Shared types, pure functions, and config/state changes that ALL user stories depend on. Must complete before any user story work.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T001 Create src/coordinare/workspace.py with WorkspaceSetupError, WorkspaceInfo dataclass (fields: path: Path | None, branch: str, repo_url: str), and WorkspaceManagerProtocol (Protocol with prepare and teardown signatures)
- [x] T002 Implement make_branch_name(card_id: str, card_title: str) -> str pure function in src/coordinare/workspace.py: pattern coordinare/{card_id}/{slug}, NFKD unicode normalization via unicodedata.normalize("NFKD", title).encode("ascii","ignore").decode(), lowercase, collapse [^a-z0-9_]+ to hyphens, strip edges, 50-char cap, .lock suffix strip, fallback "untitled"
- [x] T003 [P] Add workspace_root: Path | None = Field(default=None) and performer_image: str = Field(default="") to ProjectConfiguration in src/coordinare/config.py; add COORDINARE_WORKSPACE_ROOT and COORDINARE_PERFORMER_IMAGE to env var list in comments
- [x] T004 [P] Add workspace_manager: WorkspaceManagerProtocol | None, workspace_path: Path | None, and workspace_branch: str | None fields to CoordinareState TypedDict in src/coordinare/graph/state.py; initialize all three to None in initial_state()

**Checkpoint**: Foundation ready — workspace types, branch naming, config fields, and state fields all in place.

---

## Phase 2: User Story 1 — Ready Workspace on Dispatch (Priority: P1) 🎯 MVP

**Goal**: On dispatch, coordinare clones the repo, creates the branch, configures credentials, and injects workspace fields into the agent dispatch payload.

**Independent Test**: Dispatch a single card with a mocked WorkspaceManager; verify dispatch payload includes `repo_url`, `branch`, and `workspace_path`; verify state has `workspace_path` set after dispatch.

### Implementation

- [x] T005 [US1] Implement async _run_git(*args, cwd, env, timeout=60.0) private helper in src/coordinare/workspace.py: asyncio.create_subprocess_exec with PIPE stdout/stderr, asyncio.wait_for + communicate(), proc.kill() + await proc.wait() on TimeoutError, decode stderr with errors="replace", log stderr at DEBUG via structlog, raise GitCommandError (inner class) on non-zero returncode; catch OSError on create_subprocess_exec
- [x] T006 [US1] Implement WorkspaceManager.__init__(config: ProjectConfiguration) storing _github_org, _project_name, _github_token (SecretStr), _workspace_root (Path | None), _agent_transport (str); add _make_git_env() -> dict returning os.environ copy plus GIT_TERMINAL_PROMPT=0 in src/coordinare/workspace.py
- [x] T007 [US1] Implement WorkspaceManager.prepare(card: dict) -> WorkspaceInfo in src/coordinare/workspace.py for subprocess and SSH transports: (1) compute repo_url = f"https://github.com/{org}/{project}.git", clone_url = f"https://x-access-token:{token}@github.com/{org}/{project}.git", branch = make_branch_name(card["id"], card["title"]), (2) mkdtemp(dir=workspace_root or tempfile.gettempdir(), prefix="coordinare-ws-") as container, (3) clone_dir = container/repo, (4) git clone --depth=1 clone_url str(clone_dir) — use clone_url (with token) so private repos authenticate; do NOT log clone_url, (5) git config --local user.name "Coordinare Bot" and user.email "coordinare@localhost", (6) git remote set-url origin clone_url (keeps push auth consistent with clone), (7) git checkout -b {branch}; on any exception: shutil.rmtree(container, ignore_errors=True) then raise WorkspaceSetupError with human-readable message; return WorkspaceInfo(path=clone_dir, branch=branch, repo_url=repo_url)
- [x] T008 [P] [US1] Update AgentService.dispatch_card() in src/coordinare/services/agent_service.py to accept workspace_info: WorkspaceInfo | None = None parameter; if workspace_info is not None, add repo_url, branch, and (if path is not None) workspace_path=str(path) to the payload dict; import WorkspaceInfo using TYPE_CHECKING guard
- [x] T009 [US1] Update dispatch_card node in src/coordinare/graph/nodes/dispatch_card.py: after health check, before agent.dispatch_card(), (1) get workspace_manager from state, (2) if workspace_manager is not None: await workspace_manager.prepare(card) → workspace_info; catch WorkspaceSetupError: move card to BLOCKED with error message in open_questions, return; (3) store workspace_info.path in state["workspace_path"] and workspace_info.branch in state["workspace_branch"]; (4) pass workspace_info to agent.dispatch_card(card, workspace_info=workspace_info)
- [x] T010 [US1] Instantiate WorkspaceManager(config) in src/coordinare/__main__.py after transport/agent_service setup; inject as service_state["workspace_manager"] = workspace_manager; add import for WorkspaceManager
- [x] T011 [P] [US1] Add unit tests for WorkspaceManager.prepare() in tests/unit/test_workspace.py: (1) test_prepare_calls_git_in_order — mock _run_git to capture calls, verify clone→config identity×2→remote set-url→checkout-b order; assert clone call uses authenticated URL (x-access-token) not plain repo_url; (2) test_prepare_returns_correct_workspace_info — verify returned repo_url is plain HTTPS (no token) and branch is correct; (3) test_prepare_cleans_up_on_clone_failure — mock _run_git to raise on clone, verify mkdtemp container is removed (use tmp_path fixture); (4) test_prepare_raises_workspace_setup_error_on_failure — verify WorkspaceSetupError raised with non-empty message; (5) test_prepare_token_not_in_repo_url — assert WorkspaceInfo.repo_url does not contain the token string
- [x] T012 [P] [US1] Update tests/unit/graph/nodes/test_dispatch_card.py: add _WorkspaceManager stub with async prepare() returning fake WorkspaceInfo and async teardown() no-op; inject as state["workspace_manager"] in all existing tests; add test_dispatch_enriches_payload_with_workspace_fields verifying agent receives workspace_info; add test_dispatch_blocks_card_on_workspace_setup_error verifying WorkspaceSetupError → phase="blocked" and workspace_path not set in state
- [x] T024 [P] [US1] Update AgentServiceProtocol.dispatch_card() signature in src/coordinare/graph/state.py to accept workspace_info: WorkspaceInfo | None = None parameter, matching the AgentService change in T008; add WorkspaceInfo to TYPE_CHECKING import; must complete before T009
- [x] T025 [P] [US1] Update ResilientAgentService.dispatch_card() in src/coordinare/resilience.py to accept and forward workspace_info: WorkspaceInfo | None = None to self._inner.dispatch_card(); add WorkspaceInfo to TYPE_CHECKING import; update tests/unit/test_resilient_agent_service.py to pass workspace_info through and verify it is forwarded to the inner service; must complete before T009

**Checkpoint**: US1 complete — dispatch prepares workspace and agent receives repo_url, branch, workspace_path in payload.

---

## Phase 3: User Story 2 — Deterministic Branch Naming (Priority: P2)

**Goal**: Comprehensive verification that make_branch_name() is deterministic and git-safe across all edge cases.

**Independent Test**: Run make_branch_name() for a known card ID and title; assert result matches expected pattern; run twice and assert identical output.

### Implementation

- [x] T013 [P] [US2] Add unit tests for make_branch_name() in tests/unit/test_workspace.py: (1) test_branch_name_ascii_title — "Add retry logic" → "coordinare/ID/add-retry-logic"; (2) test_branch_name_unicode_accents — "Café résumé" → "coordinare/ID/cafe-resume"; (3) test_branch_name_special_chars_only — "---!!!" → "coordinare/ID/untitled"; (4) test_branch_name_empty_string → "coordinare/ID/untitled"; (5) test_branch_name_long_title — 200-char title → slug portion ≤50 chars; (6) test_branch_name_no_trailing_hyphen_after_truncation — title that truncates mid-word; (7) test_branch_name_lock_suffix — title whose slug ends in ".lock" gets stripped; (8) test_branch_name_deterministic — same inputs called 5 times produce identical results; (9) test_branch_name_spaces_and_caps — "Add User Auth" → "coordinare/ID/add-user-auth"
- [x] T014 [US2] Add integration test scenario for branch name verification in tests/integration/test_workspace_integration.py: clone a local bare repo (use git init --bare in tmp_path), call WorkspaceManager.prepare() for a card with a known title, assert returned branch matches make_branch_name(card_id, title), assert git -C workspace_path branch --show-current outputs the expected branch name

**Checkpoint**: US2 complete — branch naming is fully tested, deterministic, and verified against a real git repo.

---

## Phase 4: User Story 3 — Workspace Cleanup After Session Ends (Priority: P3)

**Goal**: When any session terminates (pr_opened, blocked, error, session_expired), the workspace directory is removed and workspace_path is cleared from state.

**Independent Test**: After dispatch, simulate a terminal state event in monitor_agent; assert workspace directory no longer exists and state["workspace_path"] is None.

### Implementation

- [x] T015 [US3] Implement WorkspaceManager.teardown(path: Path) -> None in src/coordinare/workspace.py: shutil.rmtree(path), log info "workspace_removed" with path; catch all exceptions, log warning "workspace_cleanup_failed" with path and error str, return without raising
- [x] T016 [US3] Update monitor_agent node in src/coordinare/graph/nodes/monitor_agent.py: at all terminal state branches (pr_opened, blocked/error/session_expired), before return: get workspace_manager and workspace_path from state; if both are set: await workspace_manager.teardown(workspace_path); set state["workspace_path"] = None and state["workspace_branch"] = None regardless of teardown outcome
- [x] T017 [P] [US3] Add unit tests for WorkspaceManager.teardown() in tests/unit/test_workspace.py: (1) test_teardown_removes_directory — use tmp_path, create a dir, call teardown, assert not exists; (2) test_teardown_warns_not_raises_on_missing_directory — teardown of nonexistent path, assert no exception raised, assert warning logged (caplog); (3) test_teardown_warns_not_raises_on_permission_error — mock shutil.rmtree to raise PermissionError, assert no exception
- [x] T018 [P] [US3] Update tests/unit/graph/nodes/test_monitor_agent.py: inject state["workspace_manager"] and state["workspace_path"] = Path("/tmp/fake-ws") in all terminal-state tests; add test_monitor_agent_teardown_called_on_pr_opened, test_monitor_agent_teardown_called_on_error, test_monitor_agent_teardown_called_on_blocked, test_monitor_agent_teardown_called_on_session_expired — each asserts teardown was called with the correct path and state["workspace_path"] is None after
- [x] T019 [US3] Add integration test for full lifecycle in tests/integration/test_workspace_integration.py: prepare workspace from local bare repo, assert directory exists, call teardown, assert directory no longer exists; also add test for teardown on nonexistent path (no raise)

**Checkpoint**: All three user stories complete — workspace setup, deterministic naming, and cleanup all verified end-to-end.

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: K8s transport path, additional integration coverage, and quickstart validation.

- [x] T020 [P] Add Kubernetes transport path to WorkspaceManager.prepare() in src/coordinare/workspace.py: when self._agent_transport == "kubernetes", skip all git operations and return WorkspaceInfo(path=None, branch=make_branch_name(card["id"], card["title"]), repo_url=f"https://github.com/{org}/{project}.git"); add unit test test_prepare_kubernetes_returns_no_path in tests/unit/test_workspace.py
- [x] T021 [P] Add integration test for workspace_root PVC simulation (quickstart scenario 6) in tests/integration/test_workspace_integration.py: create a tmp_path as simulated PVC mount, configure WorkspaceManager with workspace_root=tmp_path, prepare workspace, assert workspace_path is under tmp_path, teardown, assert tmp_path still exists
- [x] T022 [P] Add integration test for concurrent workspace isolation (quickstart scenario 3) in tests/integration/test_workspace_integration.py: use asyncio.gather to prepare two cards simultaneously from same bare repo, assert the two workspace paths are different, assert both directories exist independently, teardown both
- [x] T023 Run full test suite and verify no regressions: .venv/bin/pytest tests/unit/test_workspace.py tests/unit/graph/nodes/test_dispatch_card.py tests/unit/graph/nodes/test_monitor_agent.py tests/unit/test_resilient_agent_service.py tests/integration/test_workspace_integration.py -v; run .venv/bin/ruff check src/coordinare/workspace.py src/coordinare/config.py src/coordinare/graph/state.py src/coordinare/graph/nodes/dispatch_card.py src/coordinare/graph/nodes/monitor_agent.py src/coordinare/services/agent_service.py src/coordinare/resilience.py src/coordinare/__main__.py

---

## Dependencies & Execution Order

### Phase Dependencies

- **Foundational (Phase 1)**: No dependencies — start immediately
- **US1 (Phase 2)**: Depends on Phase 1 — all foundational tasks must complete
- **US2 (Phase 3)**: Depends on Phase 1 (make_branch_name) — can run in parallel with US1 after Phase 1
- **US3 (Phase 4)**: Depends on Phase 2 (WorkspaceManager exists) — runs after US1
- **Polish (Phase 5)**: Depends on all user stories complete

### User Story Dependencies

- **US1**: Requires Phase 1 complete (WorkspaceInfo, config fields, state fields)
- **US2**: Requires only T002 (make_branch_name) from Phase 1 — can overlap with US1 after T002 done
- **US3**: Requires US1 complete (WorkspaceManager class exists to add teardown to)

### Within Each User Story

- T005 (\_run\_git) before T006 (\_\_init\_\_) before T007 (prepare)
- T008, T024, T025 must all complete before T009 (dispatch\_card node calls agent.dispatch\_card with workspace\_info)
- T008, T011, T012, T024, T025 are independent of T007 and can run in parallel with it

### Parallel Opportunities

- T002 runs sequentially after T001 (same file); T003, T004 can run in parallel with T002 (different files)
- T008, T011, T012, T024, T025 can run in parallel with T005→T006→T007 sequence
- T013, T014 can run in parallel (US2 has no internal dependencies)
- T017, T018 can run in parallel with T015→T016 sequence
- T020, T021, T022 can all run in parallel

---

## Parallel Example: Phase 1 (Foundational)

```
# T001 → T002 (same file, sequential); then launch in parallel:
Task T003: "Add workspace_root/performer_image to config.py"
Task T004: "Add workspace fields to CoordinareState in state.py"
# T003 and T004 can overlap with T002 after T001 completes (different files)
```

## Parallel Example: User Story 1

```
# After T007 (prepare() implemented), launch in parallel:
Task T008: "Update AgentService.dispatch_card() in agent_service.py"
Task T011: "Add unit tests for WorkspaceManager.prepare() in test_workspace.py"
Task T012: "Update test_dispatch_card.py with workspace_manager mock"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Foundational (T001–T004)
2. Complete Phase 2: User Story 1 (T005–T012)
3. **STOP and VALIDATE**: Dispatch a card, inspect that the subprocess agent receives `workspace_path`, `repo_url`, `branch` in its stdin JSON
4. If valid, proceed to US2 and US3

### Incremental Delivery

1. Phase 1 → Foundation ready
2. Phase 2 (US1) → Agent receives workspace in dispatch payload (MVP)
3. Phase 3 (US2) → Branch naming fully tested and verified
4. Phase 4 (US3) → Cleanup runs on session end, no orphaned dirs
5. Phase 5 (Polish) → K8s path, PVC support, concurrency verified

---

## Notes

- [P] tasks = different files, no blocking dependencies on incomplete tasks
- `_run_git` in workspace.py must mirror SubprocessTransport pattern: kill+wait on timeout, decode stderr with errors="replace"
- Use `https://x-access-token:{token}@github.com/{org}/{project}.git` (clone_url) for both the clone step and `git remote set-url` — private repos require auth at clone time; do NOT log clone_url (contains token); `_run_git` must log stderr only, not command arguments; `repo_url` (plain HTTPS, no token) is what gets stored in WorkspaceInfo and sent in the dispatch payload
- `teardown()` must NEVER raise — log warning and return
- WorkspaceManager injected as None in tests that don't exercise workspace (backward compatible)
- Integration tests use a local bare repo (git init --bare) — no network, no GitHub token needed; each integration test function or the test module must be decorated with `@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")` so tests skip rather than error in environments without git
- The `workspace_path` in state is `Path | None`; `agent_service.py` converts to `str` when building payload
