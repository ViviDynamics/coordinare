# Implementation Plan: Reviewer Performer

**Branch**: `021-reviewer-performer` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Extend the existing performer codebase to support the `reviewing` role. The reviewer performer reads the PR diff and architecture plan, posts a structured review to the GitHub Pull Request via the Reviews API, and returns `approved` or `changes_requested`. On `changes_requested`, the coordinare relays comments to the implementer. The reviewer runs in the same performer container as the implementer — no new image needed.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `httpx` (existing, used for GitHub API calls), `pydantic>=2.9` (existing) — **no new dependencies**
**Storage**: None (reviews posted to GitHub; no performer-side persistence)
**Testing**: pytest (existing in `agent/performer/tests/`)
**Target Platform**: Linux container (base performer image)
**Performance Goals**: Review generation subject to `AGENT_TIMEOUT`; review API call < 5 s
**Constraints**: Maximum review cycle limit via `REVIEWER_MAX_CYCLES` env var (default: 3); reviewer never auto-approves without reading the diff
**Scale/Scope**: ~4 modified files in performer package, ~1 new GitHub helper, ~15 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `approved`/`changes_requested` are clean terminal states; review posting is a single GitHub API call |
| II. Testing Discipline | PASS | Unit tests required for approve path, changes-requested path, max-cycle limit, and GitHub Reviews API helper |
| III. User Experience | N/A | Internal performer |
| IV. Performance by Design | PASS | One extra GitHub API call per review cycle; negligible budget impact |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers |

## Project Structure

### Source Code (files changed)

```text
agent/performer/src/performer/
├── main.py          # MODIFIED — approved/changes_requested terminal states; REVIEWER_MAX_CYCLES limit
├── models.py        # MODIFIED — approved, changes_requested statuses; review_comments, review_cycle fields
├── github.py        # MODIFIED — add post_pull_request_review(owner, repo, pr_number, event, comments, token)
└── config.py        # MODIFIED — add REVIEWER_MAX_CYCLES setting

agent/performer/tests/unit/
├── test_main.py     # MODIFIED — reviewer path tests
└── test_github.py   # MODIFIED — post_pull_request_review tests
```

## Detailed Implementation Plan

### Step 1 — New terminal states: `approved`, `changes_requested`

**`models.py`**: Add `"approved"` and `"changes_requested"` to `PerformerStatus`. Add:

```python
review_comments: list[dict] = []  # [{file, line, body}] for changes_requested
review_cycle: int = 0             # number of review cycles exhausted
```

**`config.py`**: Add:
```python
REVIEWER_MAX_CYCLES: int = 3
```

### Step 2 — Review posting GitHub helper (`github.py`)

```python
async def post_pull_request_review(
    owner: str,
    repo: str,
    pr_number: int,
    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
    body: str,
    comments: list[dict],  # [{path, line, body}]
    token: str,
) -> dict:
    """POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews"""
```

### Step 3 — Reviewer logic in `handle_status()` (`main.py`)

When `perf.role == "reviewing"` and backend returns `done`:

```python
review_output = backend_status.output  # AI-generated review JSON
event = "APPROVE" if review_output.get("approved") else "REQUEST_CHANGES"
comments = review_output.get("comments", [])

await github.post_pull_request_review(
    owner, repo, pr_number, event=event, body=review_output.get("body", ""), comments=comments, token=token
)

if event == "APPROVE":
    perf.state = "approved"
    return PerformerResponse(status="approved", session_id=perf.session_id,
                             suggestions=review_output.get("suggestions", []))
else:
    perf.review_cycle += 1
    if perf.review_cycle >= settings.REVIEWER_MAX_CYCLES:
        perf.state = "blocked"
        perf.open_questions = [f"Review cycle limit reached ({perf.review_cycle}). Unresolved comments: ..."]
        return PerformerResponse(status="blocked", session_id=perf.session_id, questions=perf.open_questions)
    perf.review_comments = comments
    perf.state = "changes_requested"
    return PerformerResponse(status="changes_requested", session_id=perf.session_id, comments=comments)
```

### Step 4 — Architecture plan context

When dispatching the reviewer, `dispatch_card.py` includes the architecture plan content in the payload (from 020's plan-size guard). The reviewer's AI backend reads this as additional context alongside the diff.

## Complexity Tracking

No constitution violations.
