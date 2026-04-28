# Data Model: QA Visual Testing and Comment Orchestration

**Branch**: `055-qa-visual-testing-comment-orchestration` | **Date**: 2026-04-26

## New Entities

### QAScreenshotResult

Result of a single Playwright screenshot capture attempt.

```python
@dataclass
class QAScreenshotResult:
    feature_area: str           # e.g. "login", "dashboard", "card_detail"
    file_path: Path             # absolute path within workspace
    cdn_url: str | None         # GitHub CDN URL after upload; None if upload failed/skipped
    status: Literal["ok", "upload_failed", "capture_failed", "skipped"]
    error: str | None           # error message if status != "ok"
```

### IssueCommentEvent

A new comment detected on a GitHub issue linked to an active card.

```python
@dataclass
class IssueCommentEvent:
    issue_number: int
    comment_id: int
    author: str
    body: str
    created_at: datetime
    card_id: str                # coordinare card ID; set when event is created
```

### CommentClassification

Assessor output for a single comment (PR or issue).

```python
class CommentSource(str, Enum):
    pr = "pr"
    issue = "issue"

class CommentClassificationLabel(str, Enum):
    clarification = "clarification"
    scope_change = "scope_change"
    blocker_update = "blocker_update"
    approval = "approval"
    noise = "noise"

@dataclass
class CommentClassification:
    source: CommentSource
    comment_id: int
    classification: CommentClassificationLabel
    summary: str                # one-sentence assessor summary
    card_id: str
```

### DocDeduplicationResult

Output of a workspace markdown deduplication pass.

```python
@dataclass
class DocDeduplicationResult:
    files_modified: list[str]   # relative paths of files changed
    sections_merged: int        # count of duplicate sections removed
    conflicts_preserved: int    # count of ambiguous overlaps kept with disambiguated headings
```

---

## CoordinareState Extensions

New fields added to `CoordinareState` (graph/state.py):

```python
# Per-card, not global
"processed_issue_comment_ids": dict[str, set[int]]   # card_id -> set of processed comment IDs
```

---

## CardSession Extensions

New fields added to `CardSession` (session.py) and `_SESSION_FIELDS`:

```python
"last_issue_comment_id": int | None      # highest comment ID seen on linked issue
"processed_issue_comment_ids": set[int]  # idempotency guard
"qa_screenshots": list[QAScreenshotResult]  # results from most recent QA visual pass
"processed_comment_ids": set[int]        # all comment IDs (PR + issue) dispatched to assessor
```

---

## Config Extensions

New fields in `CoordinareConfig` (config.py):

```python
qa_playwright_image: str = "mcr.microsoft.com/playwright:v1.44.0-jammy"
qa_screenshot_timeout_s: int = 120       # per-screenshot timeout
qa_docker_enabled: bool = True           # set False to disable Docker entirely
qa_screenshot_upload_retries: int = 3
```

---

## Snapshot Extensions

`session_skip_reasons` already in snapshot (054). New additions to snapshot per-session:

```python
"qa_screenshots": [
    {
        "feature_area": "login",
        "cdn_url": "https://github.com/user-attachments/...",
        "status": "ok"
    }
]
```

---

## Storage

All new state is in-memory. `processed_issue_comment_ids` and `processed_comment_ids` are ephemeral per daemon run (acceptable — re-processing a comment after restart at most once is benign). `qa_screenshots` are per-cycle outputs; previous cycle results are not retained across cycles.

Screenshot files on disk are written to `{workspace_path}/qa_screenshots/` and cleaned up after CDN upload completes (or after QA cycle, whichever comes first).
