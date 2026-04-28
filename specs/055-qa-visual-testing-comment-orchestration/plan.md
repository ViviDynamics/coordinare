# Implementation Plan: QA Visual Testing and Comment Orchestration

**Branch**: `055-qa-visual-testing-comment-orchestration` | **Date**: 2026-04-26 | **Spec**: [spec.md](spec.md)

## Summary

This feature adds three orthogonal capabilities on top of the 054 foundation:

1. **Visual QA** — QA performer launches a Playwright Docker container, boots the project, and captures screenshots.
2. **CDN embedding** — Screenshots are uploaded to GitHub's asset CDN and embedded as inline images in the QA PR comment.
3. **Comment orchestration** — Coordinare polls issue comments on linked GitHub issues and routes them (alongside PR comments) through the assessor for uniform classification and dispatch.

A fourth concern — doc deduplication — is a post-write pass in the performer that merges redundant markdown sections.

No new external dependencies are required beyond `playwright` (Python library, optional — graceful degradation if absent).

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: `playwright` (Python, optional — host install), `httpx` (existing), `asyncio` (stdlib), `structlog` (existing), `pydantic-settings` (existing), `LangGraph ≥ 0.2` (existing)  
**Storage**: In-memory only; new `CardSession` fields for comment tracking and screenshot results  
**Testing**: `pytest` + `pytest-asyncio`; mock Docker/Playwright calls; mock GitHub CDN upload endpoint  
**Target Platform**: Linux daemon process with Docker available on host  
**Performance Goals**: Screenshot capture bounded to 120s timeout; CDN upload ≤ 3 retries with exponential backoff; issue comment poll adds one GitHub REST call per active card per cycle  
**Constraints**:
- Docker unavailability MUST NOT fail QA — graceful degradation required.
- GitHub CDN upload uses an unofficial API — wrap in a versioned helper, document instability risk.
- Assessor routing MUST be source-agnostic: PR and issue comments follow identical classification paths.
- Doc deduplication MUST NOT silently drop content — prefer disambiguation over deletion when overlap is ambiguous.

## Constitution Check

| Principle | Gate | Status |
|-----------|------|--------|
| I. Code Quality | Typed, single-responsibility, lint-clean | ✅ New helpers are isolated in `services/` and `graph/nodes/`; no node-bloat |
| II. Testing Discipline | Unit tests for screenshot capture, CDN upload, issue comment poll, assessor routing, dedup | ✅ Required per tasks.md |
| III. UX Consistency | Screenshot embedding follows existing PR comment format; issue comment routing follows existing PR routing | ✅ Additive |
| IV. Performance by Design | Docker launch is bounded by timeout; poll adds O(n active cards) REST calls | ✅ Acceptable overhead |
| V. Clarity Before Action | All DRs resolved in research.md | ✅ |

## Project Structure

### New files

```
src/coordinare/
  services/
    screenshot_service.py       # Docker launch, Playwright capture, CDN upload
    issue_comment_service.py    # GitHub issue comment polling + idempotency
  graph/nodes/
    qa_screenshots.py           # LangGraph node: run screenshot capture
    route_issue_comments.py     # LangGraph node: detect + classify new issue comments
  utils/
    doc_dedup.py                # Markdown deduplication pass (pure Python, no LLM)

agent/performer/
  qa/
    screenshots.py              # Playwright wrapper called by QA performer stage
    cdn_upload.py               # GitHub CDN upload helper

specs/055-qa-visual-testing-comment-orchestration/
  contracts/
    screenshot_service.md
    issue_comment_service.md
    assessor_routing.md
```

### Modified files

```
src/coordinare/
  session.py                    # +4 CardSession fields; +4 _SESSION_FIELDS entries
  graph/state.py                # +processed_issue_comment_ids global field
  config.py                     # +4 QA config fields
  graph/
    nodes/check_board.py        # extend assessor routing for issue comment source
    nodes/monitor_performer.py  # emit qa_screenshots to snapshot

agent/performer/
  qa/main.py                    # call screenshots.py after existing QA checks
  qa/report.py                  # embed CDN URLs in QA comment markdown
```

## Implementation Phases

### Phase 0 — Research & Audit (no code)

- Confirm `current_card["issue_number"]` is populated in all card shapes.
- Locate existing assessor classification logic; confirm `scope_change` label exists or add it.
- Confirm `last_pr_comment_id` pattern in `CardSession` — use as template for `last_issue_comment_id`.
- Verify GitHub CDN upload endpoint is reachable via existing `httpx` client with app token.
- Check `playwright` Python package is installable in the performer venv.

### Phase 1 — Issue Comment Polling

Add `issue_comment_service.py` with:
- `fetch_new_issue_comments(issue_number, since_id, github_client) -> list[IssueCommentEvent]`
- Idempotency: skip comment IDs already in `processed_issue_comment_ids`

Add `route_issue_comments` LangGraph node:
- Calls `issue_comment_service.fetch_new_issue_comments`
- For each new comment, calls assessor classify
- Applies dispatch actions (`requirements_changed`, performer notification, etc.)
- Updates `processed_issue_comment_ids` on session

Wire into graph: `route_issue_comments` runs in the same position as existing PR comment routing.

**Tests**: mock GitHub REST, verify idempotency, verify `scope_change` sets `requirements_changed`.

### Phase 2 — Assessor Routing Unification

Extend assessor (or add a thin adapter) so it accepts `CommentClassification` with `source` field. Verify both PR and issue comments follow the same classification path. Add `approval` and `blocker_update` labels if absent.

**Tests**: parametrize existing assessor tests with `source=pr` and `source=issue`.

### Phase 3 — Screenshot Capture

Add `screenshot_service.py`:
- `launch_docker_env(config) -> DockerSession | None` — pulls image, runs container, waits for app
- `capture_screenshots(docker_session, feature_areas) -> list[QAScreenshotResult]`
- Timeout enforcement, Docker unavailability fallback

Add `qa_screenshots` LangGraph node: calls `screenshot_service` after QA checks complete.

**Tests**: mock `subprocess`/Docker, mock `playwright` library, verify timeout path, verify graceful degradation.

### Phase 4 — CDN Upload and Embedding

Add `cdn_upload.py`:
- `upload_screenshot(path, github_client) -> str | None` — returns CDN URL or None
- Retry with exponential backoff (up to `qa_screenshot_upload_retries`)

Extend `qa/report.py`:
- Group screenshots by `feature_area`
- Embed CDN URLs as `![feature_area screenshot N](url)` in QA comment
- Replace failed uploads with `*(screenshot unavailable)*`

**Tests**: mock CDN upload endpoint, verify retry, verify comment structure with mixed ok/failed screenshots.

### Phase 5 — Doc Deduplication

Add `doc_dedup.py`:
- `find_duplicate_sections(workspace_path) -> dict[str, list[str]]` — heading → list of files containing it
- `merge_duplicate_sections(workspace_path, canonical_priority) -> DocDeduplicationResult`
- 60% token-overlap threshold for "same content"; below threshold → disambiguate headings

Wire into performer post-write hook.

**Tests**: synthetic workspace with overlapping markdown files; verify section merge; verify ambiguous overlap preserved.

### Phase 6 — Config + State Wiring

- Add new `CardSession` fields and `_SESSION_FIELDS` entries.
- Add new `CoordinareConfig` fields with defaults.
- Add `qa_screenshots` to snapshot output.
- Update dashboard to display screenshot thumbnails or count.

**Tests**: round-trip `session_to_state` / `state_to_session` with new fields.

## Risk Register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| GitHub CDN upload API changes | Medium | Wrap in versioned helper; document unofficial status; fallback to placeholder |
| Docker unavailable in CI test env | High | All Docker calls must be mockable; never require real Docker in tests |
| Playwright Python package not installed | Medium | Check at startup; emit warning; degrade gracefully |
| Issue comment polling adds latency | Low | One REST call per card per cycle; negligible vs existing board fetch |
| Doc dedup silently drops content | Medium | Disambiguation path prevents deletion; always prefer keeping content |
