# Feature Specification: QA Visual Testing and Comment Orchestration

**Feature Branch**: `055-qa-visual-testing-comment-orchestration`  
**Created**: 2026-04-26  
**Status**: Draft  
**Input**: Three orthogonal enhancements to the QA performer and coordinare loop: (1) Docker-based browser automation for visual QA, (2) GitHub CDN screenshot embedding in PR comments, and (3) assessor-driven routing for both issue and PR comments with doc deduplication.

## Background

Feature 054 delivered async multi-card orchestration and post-merge rebase dispatch. This feature builds on that foundation with three independent but synergistic improvements:

- **Visual QA**: QA performer currently relies on static analysis and CI results. This feature adds browser-based screenshot capture via a Docker-hosted Playwright environment so testers can see what the app renders.
- **Screenshot embedding**: Screenshots exist as files but must be uploaded to GitHub CDN and embedded as markdown images in PR/issue comments to be visible in review.
- **Comment orchestration**: Coordinare currently reacts to PR review comments. Issue comments (on the linked GitHub issue) are ignored. The assessor should route both comment types to the right handler.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 - QA performer captures browser screenshots via Docker (Priority: P1)

When QA runs on an open-PR branch, the QA performer should launch a Docker container with Playwright, boot the project (via README.md), log in, navigate to the feature under test, and capture screenshots.

**Why this priority**: Without visual evidence, QA reports are text-only and reviewers must mentally simulate rendering. Screenshots catch layout regressions, loading errors, and empty states that CI cannot.

**Independent Test**: Run QA on a branch with a working web app; verify a `.png` screenshot file is produced in the workspace and captured in QA output metadata.

**Acceptance Scenarios**:

1. **Given** a QA performer is running against a branch with a `docker-compose.yml` or `Dockerfile`, **When** QA stage begins, **Then** a Playwright Docker container is launched and the app is reachable inside the container.
2. **Given** a README.md with setup instructions, **When** Docker environment is ready, **Then** QA performer reads README.md, runs `docker compose up` (or equivalent), applies DB migrations, and seeds initial data.
3. **Given** the app is running, **When** QA screenshots step executes, **Then** at least one screenshot per feature area is captured and saved to the workspace.
4. **Given** Docker is unavailable or the container fails to start, **When** QA screenshot step fails, **Then** QA continues without screenshots and records a `docker_unavailable` warning in the QA report.

---

### User Story 2 - Screenshots uploaded to GitHub CDN and embedded in QA comment (Priority: P1)

After capturing screenshots, the QA performer should upload them to GitHub's asset CDN and embed them as markdown images in the QA PR comment.

**Why this priority**: GitHub renders inline images only when they are hosted on `github.com` or `githubusercontent.com`. Local paths and base64 blobs are not rendered in PR comments.

**Independent Test**: Run QA with screenshots enabled; verify the PR comment contains at least one `![...](https://github.com/user-attachments/...)` image reference.

**Acceptance Scenarios**:

1. **Given** a screenshot PNG exists in the workspace, **When** QA posts its report comment, **Then** the screenshot is uploaded via the GitHub asset upload endpoint and the returned CDN URL is embedded.
2. **Given** a screenshot upload fails (network, rate-limit), **When** embedding, **Then** the failed upload is replaced with a `*(screenshot unavailable)*` placeholder and the upload error is logged; the comment is still posted.
3. **Given** multiple screenshots, **When** the QA comment is composed, **Then** screenshots are grouped by feature area with a heading per group.
4. **Given** zero screenshots were captured (Docker unavailable), **When** QA comment is composed, **Then** the screenshot section is omitted entirely from the comment.

---

### User Story 3 - Coordinare monitors and routes issue comments (Priority: P1)

When a GitHub issue linked to an active card receives a new comment (from the assignee, a stakeholder, or the QA reviewer), coordinare should route it through the assessor for analysis and dispatch the appropriate action.

**Why this priority**: Stakeholders often leave clarifying feedback on the issue rather than the PR. Without issue comment monitoring, that context is silently dropped.

**Independent Test**: Post a comment on a linked GitHub issue saying "also make sure the button shows a loading spinner"; verify coordinare's next cycle detects the new comment and the assessor creates a `clarification` event or similar action.

**Acceptance Scenarios**:

1. **Given** an active card has a linked GitHub issue, **When** a new comment is posted on that issue, **Then** coordinare detects it within 2 poll cycles.
2. **Given** a new issue comment is detected, **When** assessor analyzes it, **Then** it is classified (clarification, scope change, blocker, noise) and the appropriate handler is invoked.
3. **Given** an issue comment was already processed in a prior cycle, **When** the same comment is seen again, **Then** it is not re-dispatched (idempotent).
4. **Given** no issue is linked to an active card, **When** poll cycle runs, **Then** no issue comment polling is attempted for that card.

---

### User Story 4 - Assessor routes both PR and issue comments uniformly (Priority: P1)

The assessor should classify and route comments from either source (PR review comment, PR general comment, issue comment) using the same routing logic, so dispatch behavior is consistent.

**Why this priority**: Two separate routing paths for PR vs issue comments would diverge over time and create inconsistent performer behavior.

**Independent Test**: Post a clarification on the issue and a clarification on the PR; verify both reach the same assessor classification path and produce equivalent dispatch actions.

**Acceptance Scenarios**:

1. **Given** a clarification posted on a PR review thread, **When** assessor processes it, **Then** it is routed to the `clarification` handler.
2. **Given** the same clarification posted on the linked issue, **When** assessor processes it, **Then** it is routed identically to the `clarification` handler.
3. **Given** a comment classified as `noise` (greeting, emoji-only, out-of-scope), **When** assessor processes it, **Then** no performer action is taken and the comment is marked processed.
4. **Given** a comment classified as `scope_change`, **When** assessor routes it, **Then** coordinare records a `requirements_changed` signal on the session and the performer is notified.

---

### User Story 5 - Doc deduplication across feature markdown files (Priority: P2)

When coordinare or a performer generates content that would be appended to multiple markdown files (spec, requirements, notes), it should detect duplication and consolidate to a single canonical location.

**Why this priority**: After many performer cycles, `requirements.md`, `notes.md`, and spec sections drift into redundancy. This wastes tokens and makes doc context unreliable.

**Independent Test**: Run coordinare on a card whose workspace has a `requirements.md` and `spec.md` with overlapping sections; verify the performer's next doc update merges rather than appends.

**Acceptance Scenarios**:

1. **Given** a workspace has multiple markdown files with overlapping sections (same heading, similar content), **When** performer prepares a doc update, **Then** the duplicate section is consolidated to the canonical file (per a priority order: spec > requirements > notes > scratch).
2. **Given** no overlap is detected, **When** performer updates docs, **Then** files are updated as normal without deduplication pass.
3. **Given** the deduplication pass encounters ambiguous overlap (same heading, different content), **When** performer decides, **Then** both versions are preserved under disambiguated headings rather than either being silently dropped.

---

### Edge Cases

- Docker image pull takes > 60s: QA should apply a configurable timeout and fall back to text-only QA.
- Screenshot file > 10 MB: compress before upload or skip with warning.
- GitHub asset upload rate-limited: retry with exponential backoff up to 3 attempts; fall back to placeholder.
- Issue comment from a bot (Copilot, Dependabot): assessor should classify as `noise` by default.
- PR and issue both receive the same comment text simultaneously: dedupe by comment body + timestamp before routing.
- App requires non-trivial login (OAuth, SSO): QA Docker environment must support seeded test credentials; fall back to `login_unavailable` warning if no test credentials are configured.
- Deduplication runs while performer has file open for editing: defer dedup until performer write is complete.

---

## Requirements *(mandatory)*

### Functional Requirements

**QA Visual Testing**

- **FR-001**: QA performer MUST detect a Docker-based browser environment (configurable; default: `mcr.microsoft.com/playwright:v1.44.0-jammy`) and launch it when QA runs on an open-PR branch.
- **FR-002**: QA performer MUST read the project `README.md`, extract setup commands, and execute them inside the Docker environment (DB migrate, seed, start server).
- **FR-003**: QA performer MUST capture at least one screenshot per feature area using Playwright's `page.screenshot()`.
- **FR-004**: QA performer MUST handle Docker unavailability gracefully: record `docker_unavailable` in QA metadata and continue without screenshots.
- **FR-005**: QA screenshot capture MUST be bounded by a configurable timeout (default: 120s); exceeding it MUST result in a `screenshot_timeout` warning, not a hard failure.

**Screenshot Embedding**

- **FR-006**: QA performer MUST upload captured screenshots to GitHub's asset upload endpoint before posting the QA comment.
- **FR-007**: Uploaded screenshot URLs MUST be embedded as inline markdown images in the QA comment using `![alt](url)` syntax.
- **FR-008**: Failed uploads MUST be replaced with `*(screenshot unavailable)*` and MUST NOT block comment posting.
- **FR-009**: Screenshots MUST be grouped by feature area with one markdown heading per group in the QA comment.

**Issue Comment Monitoring**

- **FR-010**: Coordinare MUST poll for new comments on GitHub issues linked to active cards each cycle.
- **FR-011**: Detected issue comments MUST be routed through the assessor for classification.
- **FR-012**: Issue comment processing MUST be idempotent: processed comment IDs MUST be recorded and skipped on subsequent cycles.
- **FR-013**: If no issue is linked to an active card, coordinare MUST skip issue comment polling for that card.

**Assessor Routing**

- **FR-014**: Assessor MUST classify incoming comments (from either PR or issue) as one of: `clarification`, `scope_change`, `blocker_update`, `approval`, `noise`.
- **FR-015**: `clarification` and `scope_change` comments MUST trigger a performer notification; `noise` MUST be silently recorded.
- **FR-016**: `scope_change` classification MUST set `requirements_changed = True` on the active session.
- **FR-017**: Previously-classified comment IDs MUST be stored per-session to prevent re-dispatch.

**Doc Deduplication**

- **FR-018**: When a performer prepares a markdown doc update, it MUST check for overlapping section headings across workspace markdown files.
- **FR-019**: Duplicate sections MUST be consolidated to the canonical file; duplicate in non-canonical file MUST be removed.
- **FR-020**: Ambiguous overlap (same heading, divergent content) MUST be preserved under disambiguated headings.

### Key Entities

- **QAScreenshotResult**: Outcome of one screenshot capture. Fields: `feature_area`, `file_path`, `cdn_url`, `status` (`ok` | `upload_failed` | `capture_failed`).
- **IssueCommentEvent**: A new comment detected on a linked issue. Fields: `issue_number`, `comment_id`, `author`, `body`, `created_at`.
- **CommentClassification**: Assessor output. Fields: `source` (`pr` | `issue`), `comment_id`, `classification`, `summary`.
- **DocDeduplicationResult**: Output of dedup pass. Fields: `files_modified`, `sections_merged`, `conflicts_preserved`.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: QA runs on a branch with a `docker-compose.yml` and produces at least one `QAScreenshotResult` with `status=ok`.
- **SC-002**: QA PR comment contains at least one `![...](https://github.com/user-attachments/...)` image reference.
- **SC-003**: A comment posted on a linked issue is detected and classified within 2 poll cycles.
- **SC-004**: The same issue comment is not dispatched twice across consecutive cycles.
- **SC-005**: A `scope_change` comment sets `requirements_changed=True` on the active session.
- **SC-006**: PR and issue comments with identical content are classified identically by the assessor.
- **SC-007**: Workspace markdown dedup pass removes redundant sections and the canonical file retains all content.

---

## Assumptions

- GitHub asset upload endpoint (`POST /repos/{owner}/{repo}/releases/assets` or equivalent undocumented upload endpoint) is accessible via existing `httpx` client with the repo's app token.
- Docker is installed on the QA performer host; if absent, graceful degradation applies (FR-004).
- Linked issue number is derivable from card metadata (already present in `current_card` as `issue_number` or extractable from card URL).
- The assessor is an existing LangGraph node; classification expansions are additive.
- Doc deduplication targets `*.md` files in the workspace root only; does not recurse into subdirectories.
