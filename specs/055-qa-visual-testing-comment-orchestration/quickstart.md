# Quickstart: QA Visual Testing and Comment Orchestration

**Branch**: `055-qa-visual-testing-comment-orchestration` | **Date**: 2026-04-26

## Prerequisite Setup

1. Docker installed and running on the performer host.
2. `playwright` Python package installed in performer venv: `pip install playwright && playwright install chromium`.
3. Coordinare config includes:
   ```yaml
   qa_docker_enabled: true
   qa_playwright_image: "mcr.microsoft.com/playwright:v1.44.0-jammy"
   qa_screenshot_timeout_s: 120
   qa_screenshot_upload_retries: 3
   ```
4. GitHub app token has access to the repo (existing requirement).

---

## Scenario 1: QA captures and embeds screenshots

**Setup**: Active card with open PR on a branch that has a `docker-compose.yml` and a running web app.

```bash
# Trigger QA manually via force-poll
curl -X POST http://localhost:8080/force-poll
```

**Expected**:
1. Daemon log includes `qa_screenshot.docker_launched` and `qa_screenshot.captured`.
2. PR comment on the open PR contains `![login screenshot 1](https://github.com/user-attachments/...)`.
3. Session snapshot shows `qa_screenshots: [{feature_area: "...", status: "ok", cdn_url: "..."}]`.

---

## Scenario 2: Docker unavailable — graceful degradation

**Setup**: Set `qa_docker_enabled: false` in config (or uninstall Docker).

**Expected**:
1. Daemon log includes `qa_screenshot.docker_unavailable`.
2. QA completes without screenshots; PR comment has no screenshot section.
3. Session snapshot shows `qa_screenshots: [{status: "skipped", error: "docker_unavailable"}]` or empty list.

---

## Scenario 3: Issue comment detected and routed

**Setup**: Active card with linked issue number `42`. Post a comment on issue `#42`:
```
Also make sure the submit button shows a loading spinner while the request is in flight.
```

**Expected** (within 2 poll cycles):
1. Daemon log includes `issue_comment.detected comment_id=<N> card_id=<card>`.
2. Daemon log includes `assessor.classified classification=clarification`.
3. Session `card_clarifications` list includes the new comment summary.
4. Second poll cycle: same comment ID is skipped (`issue_comment.already_processed`).

---

## Scenario 4: Scope change via issue comment

**Setup**: Active card with linked issue. Post:
```
We need to add export to CSV in addition to PDF.
```

**Expected**:
1. Assessor classifies as `scope_change`.
2. Session `requirements_changed = True`.
3. Performer receives a requirements-changed notification on next tick.

---

## Scenario 5: Doc deduplication

**Setup**: Workspace with both `spec.md` and `requirements.md` containing a `## Acceptance Criteria` section with identical content.

**Trigger**: Performer writes to both files in the same cycle.

**Expected**:
1. Post-write hook runs dedup pass.
2. `requirements.md` loses its `## Acceptance Criteria` section (non-canonical).
3. `spec.md` retains the section.
4. Daemon log includes `doc_dedup.merged sections=1`.

---

## Verifying idempotency

Post the same issue comment text twice (as two separate GitHub comments). Both will have different `comment_id`s. Both should be classified and dispatched. If the same `comment_id` is somehow seen twice (e.g. after daemon restart), only the first processing fires.

---

## Reverting

All new state is in-memory. To disable visual QA entirely without a code change:
```yaml
qa_docker_enabled: false
```

To disable issue comment polling, remove `route_issue_comments` from the graph (config flag TBD in implementation).
