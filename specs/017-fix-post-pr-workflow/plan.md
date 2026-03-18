# Implementation Plan: Post-PR Workflow Bug Fixes

**Branch**: `017-fix-post-pr-workflow` | **Date**: 2026-03-16 | **Spec**: [spec.md](./spec.md)

## Summary

Fix four confirmed bugs in the coordinare's post-PR workflow (double-dispatch loop, session-expiry routing, question loss, workspace validation), plus a fifth enhancement: the performer now waits for all CI checks to pass before reporting `pr_opened`, and auto-corrects failures up to a configurable retry limit. Coordinare-side changes are surgical (two files); performer-side changes add one new `github.py` helper, extend `Performance` model state, and modify `handle_status` logic.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (existing), asyncio (stdlib) — no new dependencies required
**Storage**: N/A — in-memory state updates only; StateStore already persists all modified fields
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project — existing `src/coordinare/` layout
**Performance Goals**: None — the changes add at most one extra `github.move_card()` call per PR-opened cycle; negligible overhead
**Constraints**: All fixes must preserve existing behaviour for the non-bug code paths (session_expired without PR, session_expired without open_questions, workspace setup success path)
**Scale/Scope**: 4 targeted coordinare changes (~15 lines total) + performer check-polling loop (~60 lines across 3 files), ~20–24 new unit tests total

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Changes are minimal, explicit, single-purpose; no cleverness |
| II. Testing Discipline | PASS | New unit tests required for every new branch (see tasks); coverage must not decrease |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | One extra async network call per PR-opened event; no budget concern |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers; root causes confirmed from production issues |

All quality gates are expected to pass. No complexity violations.

## Project Structure

### Documentation (this feature)

```text
specs/017-fix-post-pr-workflow/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── quickstart.md        # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

No `data-model.md` (no entity changes) and no `contracts/` (no API surface changes).

### Source Code (files changed)

```text
src/coordinare/graph/nodes/
├── monitor_agent.py         # MODIFIED — Bugs 1, 2, 3
└── dispatch_card.py         # MODIFIED — Bug 4 (workspace validation guard)

tests/unit/graph/nodes/
├── test_monitor_agent.py    # MODIFIED — new test cases for all three monitor_agent fixes
└── test_dispatch_card.py    # MODIFIED — new test cases for workspace validation

agent/performer/src/performer/
├── github.py                # MODIFIED — add get_check_runs() + summarise_check_runs()
├── main.py                  # MODIFIED — extend handle_status() for waiting_for_checks state
├── models.py                # MODIFIED — add pr_head_sha, check_attempt fields to Performance
└── config.py                # MODIFIED — add CHECK_MAX_ATTEMPTS setting

agent/performer/tests/unit/
└── test_github.py           # MODIFIED — tests for new check-run helpers
    test_main.py             # MODIFIED — tests for check-polling state machine
```

**Structure Decision**: No new files — all changes are targeted edits to existing files in both packages.

## Detailed Fix Plan

### Fix 1 — Missing `move_card("IN_REVIEW")` on `pr_opened` (Bug 1)

**File**: `src/coordinare/graph/nodes/monitor_agent.py`

**Location**: `monitor_agent()`, the `if marker == "pr_opened":` block (lines ~98–108).

**Current behaviour**: Sets `card["status"] = "IN_REVIEW"` in memory but never calls `github.move_card(card_id, "IN_REVIEW")`, so the GitHub board still shows IN_PROGRESS.

**Fix**: After updating card state, call `await github.move_card(card_id, "IN_REVIEW")` — mirroring the pattern already used in the `session_expired` and `blocked` handlers. Guard with `if github is not None` for defensive consistency.

**Validation rules**:
- `pr_url` and `pr_node_id` must be present in the status payload before moving. If either is absent, treat as error (do not move card or update state).

**Structural note**: The `_teardown_on_exit = True` flag is already set by default, so workspace teardown will still fire correctly after the card moves to monitoring_pr.

---

### Fix 2 — `session_expired` must check for existing PR (Bug 2)

**File**: `src/coordinare/graph/nodes/monitor_agent.py`

**Location**: `monitor_agent()`, the `elif marker == "session_expired":` block (lines ~109–126).

**Current behaviour**: Unconditionally calls `github.move_card(card_id, "TODO")` and sets `phase = "idle"` regardless of whether `card.get("pr_node_id")` is set.

**Fix**: Before moving the card to TODO, check `card.get("pr_node_id")`. If it is set (PR was successfully opened in a prior cycle):
- Do NOT move the card to TODO
- Clear `agent_dispatch` and `agent_dispatch_at` — the performer session is dead; keeping the expired `session_id` would cause `relay_feedback` to contact the dead performer and create a `monitoring_pr → relay_feedback → session_expired → monitoring_pr` loop
- Set `phase = "monitoring_pr"` and return

If `pr_node_id` is NOT set, apply the existing logic unchanged (move to TODO, clear dispatch, set phase=idle).

---

### Fix 3 — Preserve `open_questions` on `session_expired` (Bug 3)

**File**: `src/coordinare/graph/nodes/monitor_agent.py`

**Location**: Same `session_expired` block, specifically the line that clears `open_questions`.

**Current behaviour**: `state["open_questions"] = []` discards unanswered questions without saving them.

**Fix**: Before clearing, if `state.get("open_questions")` is non-empty, append a clarification entry to `card_clarifications`:

```python
open_qs = state.get("open_questions") or []
if open_qs:
    existing = list(state.get("card_clarifications") or [])
    state["card_clarifications"] = [
        *existing,
        {"questions": [str(q) for q in open_qs], "answer": ""},
    ]
state["open_questions"] = []
```

This saves the unanswered questions with an empty answer. The `assess_card` node already reads `card_clarifications` and passes them to the assessment backend and performer, so the questions will be visible on next dispatch without any further changes.

This fix applies to BOTH branches of the `session_expired` handler (i.e., whether or not `pr_node_id` is set).

---

### Fix 4 — Validate workspace context before dispatch (Bug 4)

**File**: `src/coordinare/graph/nodes/dispatch_card.py`

**Location**: After the workspace setup block (after `workspace_info` is set or remains None).

**Current behaviour**: If `workspace_manager` is present but `prepare()` raises a non-`WorkspaceSetupError` exception (any unexpected error), it is not caught in the workspace setup block and may either bubble up or get caught by the wrong outer handler. Additionally, if `workspace_info.github_token` is empty, the performer is dispatched without a token.

**Fix** (two parts):

1. **Broader exception catch**: Wrap `workspace_manager.prepare()` in `except Exception as exc` rather than only `except WorkspaceSetupError`, so any unexpected failure in workspace setup is caught and routes to BLOCKED with a descriptive message — not swallowed by the transport error handler.

2. **Post-setup completeness guard**: After `workspace_info` is set, validate the required fields before proceeding to dispatch:

```python
if workspace_manager is not None and workspace_info is not None:
    missing = []
    if not workspace_info.repo_url:
        missing.append("repo_url")
    if not workspace_info.branch:
        missing.append("branch")
    if not workspace_info.github_token:
        missing.append("github_token")
    if missing:
        reason = f"Workspace context incomplete — missing: {', '.join(missing)}"
        logger.error("dispatch_card.incomplete_workspace", card_id=card_id, missing=missing)
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception:
            logger.warning("move_card_to_blocked_failed", card_id=card_id)
        state["phase"] = "blocked"
        state["open_questions"] = [reason]
        return state
```

Note: `workspace_path` is intentionally NOT in the required list because the Kubernetes transport legitimately omits it (`path=None`). Only `repo_url`, `branch`, and `github_token` are required for the performer to function.

---

### Fix 5 — PR CI Check Validation Before Reporting `pr_opened` (US5)

**Files**: `agent/performer/src/performer/github.py`, `main.py`, `models.py`, `config.py`

**Current behaviour**: When the backend finishes (`state == "done"`), `handle_status` immediately pushes the branch, creates the PR, and returns `pr_opened`. No check on CI status.

**New behaviour**: After creating the PR, instead of returning `pr_opened`, the performer enters a `"waiting_for_checks"` intermediate state. On subsequent `status` polls it polls the GitHub Check Runs API for the head commit. Depending on the result:

- **All completed + all pass**: return `pr_opened`
- **Any still pending (queued/in_progress)**: return `working` with a progress message
- **Any failed**: relay failure details to the backend; set `perf.state = "working"` (backend fixes); increment `perf.check_attempt`; if `check_attempt >= CHECK_MAX_ATTEMPTS`: return `blocked`

**New helper functions in `github.py`**:

```python
async def get_check_runs(owner: str, repo: str, ref: str, token: str) -> list[dict]:
    """Return all check runs for a commit SHA/ref via GitHub Checks API."""
    # GET /repos/{owner}/{repo}/commits/{ref}/check-runs
    # Returns list of check run dicts with keys: name, status, conclusion, output.text

async def summarise_check_runs(
    check_runs: list[dict],
) -> tuple[Literal["pass", "fail", "pending"], list[dict]]:
    """Classify check runs into pass/fail/pending; return (verdict, failed_runs).

    - "pass": all runs completed with success/neutral/skipped
    - "fail": one or more runs completed with failure/timed_out/cancelled/action_required
    - "pending": no failures yet but some runs are still queued or in_progress
    - Empty list → "pass" (no checks configured)
    """
```

**New fields in `Performance` (models.py)**:

```python
pr_head_sha: str | None = None      # head commit SHA at time of PR creation
check_attempt: int = 0              # number of fix cycles exhausted
```

**New setting in `config.py`**:

```python
CHECK_MAX_ATTEMPTS: int = 3         # env: CHECK_MAX_ATTEMPTS
```

**`handle_status` state machine extension (main.py)**:

```python
# Guard: return a stable response for parked states without re-running backend logic.
# Without this, a coordinare poll arriving after _poll_check_runs sets
# perf.state = "blocked" would fall through to backend.get_status() → "done"
# and re-execute push + create-PR, undoing the blocked state.
if perf.state == "blocked":
    return PerformerResponse(status="blocked", session_id=perf.session_id, questions=perf.open_questions)

# Phase A: already waiting for checks — poll check runs
if perf.state == "waiting_for_checks":
    return await _poll_check_runs(perf, settings)

# Phase B: backend just finished → push + open PR + start check polling
if backend_status.state == "done":
    await push_branch(perf.stand, perf.score)
    pr_url, pr_node_id = await create_pull_request(...)
    perf.pr_url = pr_url
    perf.pr_node_id = pr_node_id
    perf.pr_head_sha = await get_head_sha(perf.stand)
    perf.state = "waiting_for_checks"
    return PerformerResponse(status="working", progress="Waiting for CI checks...", ...)

# Phase C: _poll_check_runs — poll checks (called from Phase A above)
# verdict == "pass" → perf.state = "pr_opened"; return pr_opened
# verdict == "pending" → return working
# verdict == "fail", check_attempt < max → relay failure; perf.state = "working"; increment attempt
# verdict == "fail", check_attempt >= max:
#   questions = [f"CI checks failed after {perf.check_attempt} fix attempt(s): {names}"]
#   perf.state = "blocked"; perf.open_questions = questions  ← persisted for stable re-polls
#   return blocked
```

**`head_sha` from workspace**: `push_branch` already knows the commit SHA at push time. Add `Stand.head_sha: str = ""` and populate it in `push_branch()` after the `git rev-parse HEAD` call. This avoids a separate API call.

**Transient API error during check polling**: Wrap the `get_check_runs` call in a try/except; on any exception return `working` with a progress message. Do NOT increment `check_attempt` on transient errors — only on confirmed failures.

**Check failure summary format**:
```
### {check_name}
{output.title}
{output.summary} (first 500 chars of output.text)
```

## Complexity Tracking

No constitution violations. All changes are targeted edits to existing functions.
