# Tasks: Operational Visibility & Hygiene

**Input**: Design documents from `/specs/052-stale-branch-cleanup/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, quickstart.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Foundational (Blocking Prerequisites)

**Purpose**: Config additions shared by US2 and US3 (stale branch features); must complete before those phases begin.

- [x] T001 Add `BranchCollisionStrategy(str, Enum)` with values `delete` / `suffix` and add `stale_branch_cleanup: bool = True` + `branch_collision_strategy: BranchCollisionStrategy = BranchCollisionStrategy.delete` to `ProjectConfiguration` in `src/coordinare/config.py`
- [x] T002 Write unit tests for new config fields in `tests/unit/test_config.py` — four scenarios: (a) defaults parse correctly, (b) `stale_branch_cleanup: false` disables feature, (c) `branch_collision_strategy: suffix` parses as enum, (d) env-var override `COORDINARE_STALE_BRANCH_CLEANUP=false`

**Checkpoint**: Config fields parse and default correctly; no other code changes required yet.

---

## Phase 2: User Story 1 — Backend Transparency in Dashboard (Priority: P1)

**Goal**: Operator sees a clickable link to the performer backend's live UI and a session stats summary in the dashboard performer panel.

**Independent Test**: Start coordinare with an opencode performer. Dispatch a card. Open the dashboard performer panel and verify: a clickable URL to the opencode web UI appears; the session title, files changed, and line diff counts update while the performer is active; a claude_code performer shows no link and no error.

- [x] T003 [US1] Add `SessionStats` dataclass (`title: str | None`, `files_changed: int`, `lines_added: int`, `lines_removed: int`) and `backend_ui_url: str | None` to `src/coordinare/session.py` alongside `CardSession`; add both fields to `_SESSION_FIELDS` and default them to `None` / `None` in `create_session_from_card()`
- [x] T004 [P] [US1] Implement `_discover_backend_ui_url(agent_logs: list[str]) -> str | None` in `src/coordinare/transport/subprocess_transport.py` — scan buffered `_agent_logs` for opencode's port announcement pattern (e.g. `"server":"http://127.0.0.1:<port>"`); return full URL or `None`; log `transport.backend_ui_discovered` at DEBUG when URL is found
- [x] T005 [P] [US1] Implement `_fetch_session_stats(ui_url: str) -> SessionStats | None` as an async helper in `src/coordinare/transport/subprocess_transport.py` — GET `{ui_url}/api/session` via httpx with 5 s timeout; map response fields to `SessionStats`; return `None` on any error; log `transport.session_stats_error` at DEBUG on failure
- [x] T006 [US1] Wire backend transparency into `src/coordinare/graph/nodes/monitor_performer.py` — after each stderr drain, call `_discover_backend_ui_url(transport.agent_logs)`; if URL found, call `_fetch_session_stats` (throttled to ≥30 s between calls using a timestamp); write `backend_ui_url` and `session_stats` into the active `CardSession`; explicitly set both to `None` when the session ends (performer exits or is cancelled)
- [x] T006a [US1] Write unit tests for the T006 wiring logic in `tests/unit/test_monitor_performer.py` — (a) URL discovered after stderr log appears → written to session, (b) stats fetch throttled: second call within 30 s skips HTTP request, (c) session-end path clears `backend_ui_url` and `session_stats` to `None`
- [x] T007 [US1] Extend `DashboardStore._build_state()` in `src/coordinare/dashboard.py` to include `backend_ui_url` and `session_stats` (as a dict) per active performer entry in the SSE state payload
- [x] T008 [US1] Update the performer panel in the dashboard HTML/JS in `src/coordinare/dashboard.py` — render `backend_ui_url` as `<a href="{url}" target="_blank">Open in browser ↗</a>` when non-null; render `session_stats` as `"{title} · {files} files · +{added}/-{removed} lines"` when non-null; hide both when null
- [x] T009 [P] [US1] Write unit tests in `tests/unit/test_subprocess_transport.py` — (a) `_discover_backend_ui_url` returns URL from matching log line, (b) returns `None` when no matching line, (c) `_fetch_session_stats` returns `SessionStats` on success, (d) returns `None` on HTTP error without raising

**Checkpoint**: Dashboard performer panel shows live backend URL and session stats for opencode; degrades gracefully for other backends.

---

## Phase 3: User Story 2 — Stale Branch Delete (Priority: P1)

**Goal**: Stale remote branch detected and deleted before workspace creation; 422 PR failures eliminated.

**Independent Test**: Manually create branch `coordinare/CARD-89/add-auth` on the remote. Dispatch card #89. Verify coordinare logs `workspace.stale_branch_delete_attempted`, the branch is deleted, workspace is created cleanly, and PR is successfully created.

- [x] T010 [P] [US2] Add `branch_exists(self, branch_name: str) -> bool` to `GitHubService` in `src/coordinare/services/github.py` — `GET /repos/{owner}/{repo}/branches/{branch_name}` via httpx; return `True` on 200, `False` on 404; derive REST base from existing `_github_api_url` config (GHE-compatible)
- [x] T011 [P] [US2] Add `delete_branch(self, branch_name: str) -> None` to `GitHubService` in `src/coordinare/services/github.py` — `DELETE /repos/{owner}/{repo}/git/refs/heads/{branch_name}` via httpx; on failure log `workspace.stale_branch_delete_failed` at WARNING and return (do not raise)
- [x] T012 [US2] Accept optional `github_service: GitHubService | None = None` in `WorkspaceManager.__init__()` in `src/coordinare/workspace.py` and store as `self._github_service`; update construction in `src/coordinare/__main__.py` to pass the existing `github_service` instance
- [x] T013 [US2] Add stale branch detection to `WorkspaceManager.prepare()` in `src/coordinare/workspace.py` — after `branch` is computed, if `stale_branch_cleanup` is true and `_github_service` is set, call `branch_exists(branch)`; if branch exists and strategy is `delete`, call `delete_branch(branch)` and log `workspace.stale_branch_delete_attempted` (INFO, fields: `branch`, `card_id`); skip silently when no github_service (test/health path)
- [x] T014 [P] [US2] Write unit tests for `branch_exists` and `delete_branch` in `tests/unit/test_github_service.py` — mock httpx responses: (a) 200 → True, (b) 404 → False, (c) DELETE 204 → success, (d) DELETE 422 → logs warning, does not raise
- [x] T015 [US2] Write unit tests for stale branch detection in `tests/unit/test_workspace.py` — (a) stale branch exists + strategy delete → `delete_branch` called before clone, (b) no stale branch → workspace created normally, (c) `stale_branch_cleanup: false` → no check performed, (d) deletion failure → warning logged, workspace creation continues

**Checkpoint**: Dispatch with pre-existing remote branch → branch deleted, clean workspace created.

---

## Phase 4: User Story 3 — Branch Collision Suffix Strategy (Priority: P2)

**Goal**: Operators can choose suffix mode so stale branches are renamed rather than deleted.

**Independent Test**: Set `branch_collision_strategy: suffix`. Dispatch the same card while its previous branch still exists. Verify the new workspace uses a `-2` branch name and the original branch is untouched.

- [x] T016 [US3] Extend stale branch handling in `WorkspaceManager.prepare()` in `src/coordinare/workspace.py` — when strategy is `suffix` and stale branch exists, iterate candidates `{branch}-2` through `{branch}-9`; use the first available; if all taken, fall back to delete and log `workspace.suffix_exhausted_delete_attempted` (WARNING); log `workspace.branch_suffix_applied` (INFO, fields: `original_branch`, `final_branch`, `card_id`) on success
- [x] T017 [US3] Write unit tests for suffix strategy in `tests/unit/test_workspace.py` — (a) suffix applied when `-2` is free, (b) suffix skips taken candidates and uses first free slot, (c) all suffixes taken → falls back to delete + warning, (d) `WorkspaceInfo.branch` reflects final suffixed name

**Checkpoint**: With `branch_collision_strategy: suffix`, second dispatch for same card creates `-2` branch; original branch preserved.

---

## Phase 5: User Story 4 — Contract Enforcement in Speckit (Priority: P2)

**Goal**: `speckit.analyze` flags unregistered cross-boundary payload fields; maintenance command compares contracts against code.

**Independent Test**: Run `speckit.analyze` on a spec that mentions a new field `foo_bar` in payload-change context but does not update `specs/contracts/dispatch-payload.md`. Verify a `[CONTRACT MISMATCH]` warning appears in the output listing `foo_bar`.

- [x] T018 [US4] Extend the `speckit.analyze` skill at `.specify/skills/speckit.analyze` — add a **Contract Check** step: (a) read all `specs/contracts/*.md` files; (b) parse each `## Field Registry` section's markdown table to extract known field names per contract; (c) scan the feature's `spec.md` and `plan.md` for new field names appearing in payload-change context; (d) emit `[CONTRACT MISMATCH] Field '{field}' referenced in spec/plan but not registered in {contract_file}` for each unregistered field; (e) skip this check silently if no contract files exist
- [x] T019 [US4] Update `specs/contracts/dispatch-payload.md` to add a `## How to use` section documenting the contract format — field registry table structure, how to add new fields, and how `speckit.analyze` uses the file — so spec authors know to update it when changing cross-boundary payloads
- [x] T020 [P] [US4] Create `.specify/skills/speckit.contracts` skill — reads all `specs/contracts/*.md` field registries; greps the coordinare source code for corresponding Pydantic model classes (e.g. `Score`, `ProtocolMessage`, `ProtocolResponse`); reports: (a) fields in code but absent from contract, (b) fields in contract but absent from code; outputs a diff-style summary for manual review

**Checkpoint**: `speckit.analyze` on a spec with an unregistered payload field produces a `[CONTRACT MISMATCH]` warning; `speckit.contracts` reports any drift between code and contract registry.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Documentation alignment and final validation.

- [x] T021 Update `specs/052-stale-branch-cleanup/data-model.md` to add `SessionStats` dataclass fields, `backend_ui_url` SSE field, and new log events: `transport.backend_ui_discovered` (DEBUG), `transport.session_stats_error` (DEBUG)
- [x] T022 [P] Verify new dashboard link (`<a href=…>`) and session stats text have visible focus indicator and meet WCAG 2.1 AA contrast in `src/coordinare/dashboard.py` — adjust CSS if needed
- [x] T023 [P] Validate all four quickstart scenarios from `specs/052-stale-branch-cleanup/quickstart.md` against implemented behavior and update any description mismatches

---

## Dependencies & Execution Order

### Phase Dependencies

- **Foundational (Phase 1)**: No dependencies — start immediately
- **US1 — Backend Transparency (Phase 2)**: Independent of Phase 1 — can start in parallel with Phase 1
- **US2 — Stale Branch Delete (Phase 3)**: Depends on Phase 1 (config fields) — start after T001–T002
- **US3 — Suffix Strategy (Phase 4)**: Depends on Phase 3 (stale branch delete logic) — start after T015
- **US4 — Contract Enforcement (Phase 5)**: Independent of all above — can start any time
- **Polish (Phase 6)**: Depends on all story phases complete

### User Story Dependencies

- **US1**: Fully independent — no dependency on US2/US3/US4
- **US2**: Depends on Foundational (T001)
- **US3**: Extends US2 — depends on T013 (stale branch detection)
- **US4**: Fully independent — operates only on speckit skill files and contracts/

### Parallel Opportunities

- T004 and T005 (US1 helpers) can be written in parallel — different scopes in same file
- T010 and T011 (US2 GitHub methods) can be written in parallel — same file, no dependency between them
- T014 (GitHub service tests) can be written alongside T010/T011 (TDD)
- Phase 2 (US1) and Phase 1 (Foundational) can run concurrently
- Phase 5 (US4) can run concurrently with any other phase

---

## Parallel Example: User Story 2

```bash
# These can be worked in parallel (different methods, same file):
Task T010: branch_exists() in src/coordinare/services/github.py
Task T011: delete_branch() in src/coordinare/services/github.py
Task T014: Unit tests for both in tests/unit/test_github_service.py
```

---

## Implementation Strategy

### MVP First (US2 — Stale Branch Delete) 🎯

US2 is the highest-impact, lowest-risk change: it directly fixes the 422 PR failure that blocks live testing.

1. Complete Phase 1 (T001–T002): config fields
2. Complete Phase 3 (T010–T015): stale branch delete
3. **STOP and VALIDATE**: dispatch a card with a pre-existing branch; confirm clean workspace
4. Optionally continue to US1 (backend transparency) and US3 (suffix strategy)

### Incremental Delivery

1. Phase 1 + Phase 3 → Stale branch cleanup (immediate operator unblock)
2. Phase 4 (US3) → Suffix strategy (power-user option)
3. Phase 2 (US1) → Backend transparency (dashboard enrichment)
4. Phase 5 (US4) → Contract enforcement (speckit tooling improvement)

Each phase is independently shippable without breaking prior work.
