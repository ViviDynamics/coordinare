# Tasks: QA Visual Testing and Comment Orchestration

**Branch**: `055-qa-visual-testing-comment-orchestration` | **Date**: 2026-04-26

## Phase 0 — Research & Audit

- [x] Confirm `current_card["issue_number"]` is populated across all active card shapes (board fetcher output)
- [x] Locate assessor classification logic in `graph/nodes/`; confirm `scope_change` and `approval` labels or plan to add them
- [x] Confirm `last_pr_comment_id` pattern in `CardSession` / `CoordinareState` for use as template
- [x] Verify GitHub CDN upload endpoint flow (3-step: policies → S3 → attachments) with manual curl against the repo
- [x] Verify `playwright` Python package is installable in `agent/performer/.venv`

## Phase 1 — Issue Comment Polling

- [x] Add `IssueCommentEvent` dataclass to `src/coordinare/services/issue_comment_service.py`
- [x] Implement `fetch_new_issue_comments(issue_number, since_id, github_client)` with REST call and idempotency
- [x] Add `processed_issue_comment_ids: set[int]` and `last_issue_comment_id: int | None` to `CardSession` and `_SESSION_FIELDS`
- [x] Add `route_issue_comments` LangGraph node in `graph/nodes/route_issue_comments.py`
- [x] Wire `route_issue_comments` into the coordinare graph (after PR comment routing, before session tick)
- [x] Apply `scope_change` dispatch: set `requirements_changed=True` on session when classification is `scope_change`
- [x] Apply `clarification` dispatch: add entry to `card_clarifications` on session
- [x] Write unit tests: mock GitHub REST, verify new comments detected, verify idempotency, verify `scope_change` → `requirements_changed`

## Phase 2 — Assessor Routing Unification

- [x] Add `source: Literal["pr", "issue"]` field to `CommentClassification` dataclass
- [x] Extend assessor node/service to accept and emit `CommentClassification` with `source`
- [x] Add `approval` and `blocker_update` classification labels if not present
- [x] Parametrize existing assessor tests with `source=pr` and `source=issue` to verify uniform behavior
- [x] Write unit test: PR clarification and issue clarification produce same classification label

## Phase 3 — Screenshot Capture

- [x] Add `QAScreenshotResult` dataclass to `agent/performer/qa/screenshots.py`
- [x] Implement `launch_docker_env(config) -> DockerSession | None` — pull image, run container, wait for app
- [x] Implement `capture_screenshots(docker_session, feature_areas, timeout_s) -> list[QAScreenshotResult]`
- [x] Add graceful fallback: if Docker unavailable, return `QAScreenshotResult(status="skipped", error="docker_unavailable")`
- [x] Add `qa_screenshots` LangGraph node in `graph/nodes/qa_screenshots.py`; call after QA checks pass
- [x] Add `qa_screenshots: list[QAScreenshotResult]` to `CardSession` and `_SESSION_FIELDS`
- [x] Add `qa_docker_enabled`, `qa_playwright_image`, `qa_screenshot_timeout_s` to `CoordinareConfig`
- [x] Write unit tests: mock Docker subprocess, mock playwright, verify timeout path, verify graceful degradation when Docker absent

## Phase 4 — CDN Upload and Embedding

- [x] Implement `upload_screenshot(path, github_client) -> str | None` in `agent/performer/qa/cdn_upload.py`
- [x] Add retry logic: exponential backoff, up to `qa_screenshot_upload_retries` attempts
- [x] Extend `agent/performer/qa/report.py`: group screenshots by `feature_area`, embed CDN URLs as `![alt](url)`
- [x] Embed `*(screenshot unavailable)*` for failed uploads; omit screenshot section entirely if no screenshots
- [x] Add `qa_screenshot_upload_retries: int = 3` to `CoordinareConfig`
- [x] Write unit tests: mock CDN upload (success path, retry path, all-fail path), verify comment markdown structure

## Phase 5 — Doc Deduplication

- [x] Implement `find_duplicate_sections(workspace_path) -> dict[str, list[str]]` in `src/coordinare/utils/doc_dedup.py`
- [x] Implement `merge_duplicate_sections(workspace_path, canonical_priority) -> DocDeduplicationResult`
- [x] Enforce canonical priority: `spec.md > requirements.md > notes.md > scratch.md > other`
- [x] Implement ambiguous-overlap handling: keep both under disambiguated headings
- [x] Wire dedup pass as a post-write hook in performer (after performer writes multiple `.md` files)
- [x] Write unit tests: synthetic workspace with overlapping sections; verify merge; verify ambiguous case preserved

## Phase 6 — Config, State Wiring, Dashboard

- [x] Add all new `CardSession` fields to `_SESSION_FIELDS` (verify round-trip in `session_to_state` / `state_to_session`)
- [x] Add `qa_screenshots` to daemon snapshot output
- [x] Update dashboard to display screenshot count or thumbnail links per session
- [x] Write round-trip test for new `CardSession` fields

## Phase 7 — Integration Tests

- [x] End-to-end test: QA with mocked Docker + mocked CDN produces PR comment with embedded screenshot markdown
- [x] End-to-end test: issue comment posted → detected next cycle → classified → `requirements_changed=True`
- [x] End-to-end test: same issue comment processed twice → only dispatched once (idempotency)
- [x] End-to-end test: dedup pass on workspace with `spec.md` + `requirements.md` overlap → merged correctly

## Phase 8 — Backend-Agnostic Performer Tuning (effort, temperature, max_tokens)

Goal: allow operators to tune each performer role's quality, creativity, and cost without knowing backend
internals. Three new fields on `PerformerRoleConfig` are translated at dispatch time to whatever each
backend actually accepts.

### Config

- [x] Add `effort: Literal["low", "medium", "high"] | None = None` to `PerformerRoleConfig`
- [x] Add `temperature: float | None = None` to `PerformerRoleConfig` (0.0–1.0; None = backend default)
- [x] Add `max_tokens: int | None = None` to `PerformerRoleConfig` (None = backend default)
- [x] Add field validators: `temperature` must be in [0.0, 1.0]; `max_tokens` must be > 0

### Translation layer

- [x] Create `src/coordinare/services/performer_tuning.py` with `translate_tuning(role_config, backend) -> dict[str, Any]`
  - **effort**
    - `opencode` → `{"effort": effort}`
    - `claude` / `anthropic` → `{"thinking": {"type": "enabled", "budget_tokens": N}}` (low=1024, medium=8192, high=32000)
    - Unknown backend → pass through `{"effort": effort}` + log warning
  - **temperature**
    - All backends → `{"temperature": value}` (universally understood; log warning if backend is unknown)
  - **max_tokens**
    - `opencode` → `{"max_tokens": value}`
    - `claude` / `anthropic` → `{"max_tokens": value}`
    - Unknown backend → pass through + log warning
  - Returns a merged dict; omits keys for fields that are `None`
- [x] Unit-test each field × each backend branch, plus all-None returns empty dict

### Dispatch wiring

- [x] In `dispatch_performer.py`, call `translate_tuning(role_config, role_config.backend)` and merge result into `card_context`
- [x] Unit-test: all None → no tuning keys in payload; effort="high" + temperature=0.2 + max_tokens=4096 + backend="opencode" → correct keys

### Integration

- [x] Integration test: config with `effort: high`, `temperature: 0.3`, `max_tokens: 8192` for implementer → dispatch payload contains all translated keys
- [x] Verify no regression when all three fields are omitted (existing configs unchanged)

## Phase 9 — Tuning End-to-End Wiring + Token-Cap Blocked Handling

Goal: close the gap between coordinare dispatch and actual backend execution — tuning fields must
reach the CLI/API layer — and surface token-cap exhaustion as a human-actionable blocked state.

### Gap 1: Score model (performer receives tuning fields)

- [x] Add `effort: str = ""`, `temperature: float | None = None`, `max_tokens: int | None = None` to `Score` in `agent/performer/src/performer/models.py`
- [x] Remove `extra="ignore"` comment referencing these fields once they are explicit

### Gap 2: BackendAdapter protocol + implementations

- [x] Add `effort`, `temperature`, `max_tokens` kwargs to `BackendAdapter.start()` in `backends/base.py`
- [x] Add `stop_reason: str | None = None` to `BackendStatus` (values: `"max_tokens"`, `"end_turn"`, etc.)
- [x] `claude_code` backend: store tuning kwargs; add `--max-tokens N` CLI arg when `max_tokens` is set; detect `stop_reason: "max_tokens"` in `result` event → set `BackendStatus(state="error", stop_reason="max_tokens")`
- [x] `opencode` backend: pass `modelID` (from `model` kwarg) in `POST /session` body when set; pass `max_tokens` in session body when set; log a warning for `effort` (not yet in opencode HTTP API)
- [x] `main.py`: thread `score.effort`, `score.temperature`, `score.max_tokens` through to `backend.start()` at both dispatch and retry paths (lines ~524, ~628)

### Gap 3: claude_code backend name in tuning translation

- [x] Add `"claude_code"` to `_ANTHROPIC_BACKENDS` in `src/coordinare/services/performer_tuning.py`

### Gap 6: Token-cap blocked handling (coordinare side)

- [x] Add `"token_limit"` to `PerformerStatusType` in `agent/performer/src/performer/protocol.py`
- [x] Add `"token_limit"` to `StatusType` in `src/coordinare/protocol.py`
- [x] In `agent/performer/src/performer/main.py`: when `backend_status.stop_reason == "max_tokens"`, return `PerformerResponse(status="token_limit", reason="...")` instead of generic `"error"`
- [x] In `src/coordinare/graph/nodes/monitor_performer.py`: handle `marker == "token_limit"` → move card to BLOCKED, set `open_questions` to a human-readable message naming the role and current `max_tokens` value, and suggesting `max_tokens: 0` to remove the cap
- [x] Post a GitHub issue comment (via existing `github.post_comment`) explaining the cap was hit

### Unlimited max_tokens sentinel

- [x] Fix `resolved_role()` inheritance: `max_tokens: 0` in a role config overrides the default's `max_tokens` and resolves to `None` (unlimited)
- [x] Validator updated: `max_tokens >= 0`; `0` = unlimited sentinel, resolved to `None` by `resolved_role()`
- [x] `translate_tuning()` passes through `None` (omits key) for the unlimited case
- [x] Documented in config comments

### Tests

- [x] Unit test: `Score` accepts `effort`, `temperature`, `max_tokens` from payload; extra fields still ignored
- [x] Unit test: `claude_code` backend passes `--max-tokens` arg when `max_tokens` is set
- [x] Unit test: `claude_code` backend sets `stop_reason="max_tokens"` on `BackendStatus` when result event contains `stop_reason: "max_tokens"`
- [x] Unit test: `monitor_performer` routes `token_limit` status → `phase="blocked"` with descriptive `open_questions`
- [x] Unit test: `resolved_role()` — role with explicit `max_tokens=0` overrides default's `max_tokens: 8192` → resolved has `max_tokens=None`
