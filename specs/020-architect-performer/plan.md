# Implementation Plan: Architect Performer

**Branch**: `020-architect-performer` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Extend the existing performer codebase (`agent/performer/`) to support the `architecting` role. The architect performer analyses the codebase and card requirements, commits a structured Markdown plan file to the feature branch, and returns `plan_committed` as its terminal success state. All changes are within `agent/performer/`; the coordinare lifecycle (019) already handles advancement on receipt of `plan_committed`. No new container images are needed — the architect uses the same base performer image.

## Technical Context

**Language/Version**: Python 3.12+ (performer) + shell (git operations)
**Primary Dependencies**: `httpx` (existing), `psutil` (existing), `pydantic>=2.9` (existing), `structlog>=24.1` (existing) — **no new dependencies**
**Storage**: None (plan file committed to feature branch via git; no performer-side persistence)
**Testing**: pytest (existing in `agent/performer/tests/`)
**Target Platform**: Linux container (same base image as implementer performer)
**Performance Goals**: Plan generation time subject to `AGENT_TIMEOUT` (default 30 min); `plan_committed` response within one status poll after backend completes
**Constraints**: Plan file path is fixed and predictable (`docs/coordinare-architecture.md`); re-runs overwrite rather than append; plan file > 32 KB bypasses inline payload injection (coordinare reads from branch instead)
**Scale/Scope**: ~3 modified files in `agent/performer/src/performer/`, ~2 new files, ~15 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `plan_committed` is a new terminal state with single responsibility; no role-specific logic leaks into protocol layer |
| II. Testing Discipline | PASS | Unit tests required for plan-commit path, overwrite behavior, and blocked-on-ambiguity path |
| III. User Experience | N/A | Internal performer; no UI changes |
| IV. Performance by Design | PASS | Plan file commit is a single git operation; negligible overhead |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers |

## Project Structure

### Source Code (files changed)

```text
agent/performer/src/performer/
├── main.py          # MODIFIED — handle plan_committed terminal state; add handle_plan_commit()
├── models.py        # MODIFIED — add plan_committed status; add plan_path field to Performance
├── workspace.py     # MODIFIED — add commit_file(path, content, message) helper
└── config.py        # MODIFIED — add PLAN_FILE_PATH, PLAN_PAYLOAD_MAX_BYTES settings

agent/performer/tests/unit/
├── test_main.py     # MODIFIED — tests for plan_committed path
└── test_workspace.py # MODIFIED — tests for commit_file()
```

**Structure Decision**: No new files — all changes are targeted edits within the existing performer package. The architect role is a behavioral variant of the existing performer, not a new binary.

## Detailed Implementation Plan

### Step 1 — New terminal state: `plan_committed`

**`models.py`**: Add `"plan_committed"` to the `PerformerStatus` literal type. Add `plan_path: str | None = None` to `Performance` (populated after successful commit).

**`main.py`**: In `handle_status()`, after backend returns `done`:

```python
if backend_status.state == "done":
    # Architect path: commit plan file instead of opening a PR
    if perf.role == "architecting":
        plan_content = backend_status.output  # AI-generated Markdown
        plan_path = settings.PLAN_FILE_PATH   # default: "docs/coordinare-architecture.md"
        await git.commit_file(perf.stand, plan_path, plan_content, "chore: add architecture plan")
        perf.plan_path = plan_path
        perf.state = "plan_committed"
        return PerformerResponse(status="plan_committed", plan_path=plan_path, session_id=perf.session_id)
    else:
        # Existing implementer path
        await push_branch(...)
        ...
```

**`config.py`**: Add:
```python
PLAN_FILE_PATH: str = "docs/coordinare-architecture.md"
PLAN_PAYLOAD_MAX_BYTES: int = 32_768  # 32 KB; beyond this, coordinare reads from branch
```

### Step 2 — `commit_file()` git helper (`workspace.py`)

```python
async def commit_file(stand: Stand, path: str, content: str, message: str) -> None:
    """Write content to path in the stand's repo and commit it.

    If the file already exists, it is overwritten (re-run safety).
    """
    abs_path = stand.path / path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    await _git(stand, "add", str(abs_path))
    # Only commit if there are staged changes (idempotency)
    diff = await _git(stand, "diff", "--cached", "--quiet", check=False)
    if diff.returncode != 0:
        await _git(stand, "commit", "-m", message)
    await _git(stand, "push", "origin", stand.branch)
```

### Step 3 — Role field on Performance

Add `role: str = "implementing"` to the `Performance` model. The coordinare sets this in the dispatch payload (via `performer_stage` in 019). The architect path in `handle_status` gates on `perf.role == "architecting"`.

### Step 4 — Plan path in dispatch payload (coordinare side)

In `src/coordinare/graph/nodes/dispatch_performer.py`, when building the dispatch payload for any role after the architect, include the plan's branch-relative path:

```python
plan_path = card.get("plan_path")
if plan_path and performer_stage != "architecting":
    card_context["architecture_plan_path"] = plan_path
```

Downstream performers read the plan content directly from the branch. This avoids payload size concerns entirely.

## Complexity Tracking

No constitution violations.
