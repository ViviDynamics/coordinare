# Tasks: Performer Hardening

**Input**: Design documents from `specs/013-performer-hardening/`
**Prerequisites**: spec.md ✓

---

## User Story 1 — Secure Credential Passing

- [X] T001 Add `git_env: dict[str, str]` field to `Stand` dataclass in `agent/performer/src/performer/models.py`; default empty dict; populated after clone
- [X] T002 Implement `_git_credential_vars(token)` in `agent/performer/src/performer/workspace.py` returning only git-specific env vars (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0` as `Authorization: Basic <base64(x-access-token:{token})>`, plus trace-disable vars `GIT_TRACE=0`, `GIT_TRACE2=0`, `GIT_TRACE_CURL=0`, `GIT_CURL_VERBOSE=0`); populate `stand.git_env` at end of `clone_repository()`
- [X] T003 Pass `env={**os.environ, **stand.git_env}` to the AI subprocess in `ClaudeCodeAdapter._launch()`, `OpenCodeAdapter.start()`, and `CodexAdapter.start()`
- [X] T004 Add prompt footer to all three backends: `"Do not push or open a pull request — this will be handled automatically after you finish."` (removes misleading instructions that implied the agent should push)
- [X] T005 Write unit tests verifying `Stand.git_env` is populated after `clone_repository()` and that credentials do not appear in raised exceptions (redaction assertion)

---

## User Story 2 — System Error Retry Loop

- [X] T006 Add `"system_error"` to the `phase` `Literal` in `src/coordinare/graph/state.py`; add state fields `system_error_count: int`, `system_error_last_at: datetime | None`, `system_error_reason: str | None`, `system_error_notified: bool`; initialise all in `initial_state()`
- [X] T007 Add `performer_error = "performer_error"` to `EventType` enum in `src/coordinare/models/notification.py`
- [X] T008 Implement `handle_system_error` node in `src/coordinare/graph/nodes/handle_system_error.py`: constants `_MAX_RETRIES=3`, `_RETRY_INTERVAL=90.0s`; if `count < _MAX_RETRIES` and interval not elapsed → `phase="idle"` (wait); if elapsed → clear dispatch, `phase="dispatching"` (retry); if `count >= _MAX_RETRIES` and not notified → send `NotificationEvent(type=performer_error, severity=critical)`, move card to `BLOCKED`, set `system_error_notified=True`, `phase="idle"`
- [X] T009 Update `monitor_agent` node: `marker == "error"` → set `system_error_count++`, `system_error_last_at`, `system_error_reason`, `phase="system_error"` (was: `phase="blocked"` with open_questions); success path (`pr_opened`) clears all system error state
- [X] T010 Update `dispatch_card` node: `result.get("status") == "error"` → set system_error fields, `phase="system_error"` (was: `phase="blocked"`); success path clears system error state
- [X] T011 Update `check_board` node: `in_progress` branch → if `system_error_count > 0` then `phase="system_error"` (not `monitoring_agent`); `blocked` branch → if `system_error_notified` then `phase="idle"` (skip re-notifying via `handle_blocked`)
- [X] T012 Update routing in `src/coordinare/graph/routing.py`: add `"system_error"` handling to `route_from_board_check`, `route_from_agent_status`, `route_from_dispatch`; add new `route_from_system_error` returning `"dispatch"` for `"dispatching"` phase, `"idle"` otherwise
- [X] T013 Wire `handle_system_error` into graph builder in `src/coordinare/graph/builder.py`: add node, conditional edges from `check_board`, `monitor_agent`, `dispatch_card`; `handle_system_error` routes to `dispatch_card` or END
- [X] T014 [P] Update `TransportError` handling in `dispatch_card`: route to `system_error` instead of `blocked`; keep `PermanentGitHubError` as immediate `blocked`
- [X] T015 [P] Update `TransportError` handling in `monitor_agent`: route to `system_error` instead of `blocked`; keep `PermanentGitHubError` as immediate `blocked`
- [X] T016 [P] Update `dispatch_card` health check: `"unreachable"` (exception thrown) treated same as `"unknown"` → `phase="idle"` (transient); only explicit `"error"` status → `phase="blocked"`
- [X] T017 Write `tests/unit/graph/nodes/test_handle_system_error.py`: 13 tests covering wait-when-interval-not-elapsed, retry-when-elapsed, no-timestamp-retry, max-retries-notification, no-duplicate-notification, missing-services-handled-gracefully
- [X] T018 [P] Update `tests/unit/graph/nodes/test_dispatch_card.py`: unreachable agent → idle; TransportError → system_error
- [X] T019 [P] Update `tests/unit/graph/nodes/test_monitor_agent.py`: TransportError → system_error; update teardown tests
- [X] T020 [P] Update `tests/unit/graph/test_routing.py`: add `system_error` cases for existing route functions; add `test_route_from_system_error_paths`
- [X] T021 Update integration test `test_dispatch_error_enters_retry_cycle` (was `test_dispatch_error_posts_reason`): assert `phase=="idle"`, `system_error_count==1`, no GH comments, card not moved to BLOCKED

---

## User Story 3 — Resilient Push and PR Creation

- [X] T022 Change `push_branch()` in `agent/performer/src/performer/workspace.py` from `--force-with-lease` to `--force`; document why in the docstring (no named remote → no tracking ref → `(stale info)` with lease)
- [X] T023 Add `get_existing_pull_request(owner, repo, branch, token)` to `agent/performer/src/performer/github.py`: `GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open`; returns `(html_url, node_id)` of first result; raises `GitHubAPIError(404, ...)` if none found
- [X] T024 Update `create_pull_request()`: catch HTTP 422 with `"already exists"` in errors array → call `get_existing_pull_request()` instead of raising; log `"pull request already exists, fetching existing PR"`
- [X] T025 Update unit tests in `agent/performer/tests/unit/test_github.py`: test 422 "already exists" → returns existing PR; test `get_existing_pull_request` happy path; test `get_existing_pull_request` 404 when no PR found
