# Implementation Plan: Tech Writer Performer

**Branch**: `024-tech-writer-performer` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Extend the existing performer codebase to support the `documenting` role. The tech writer performer analyses the feature branch diff, detects existing documentation conventions, produces a CHANGELOG entry and README updates, adds inline docstrings for new/changed public interfaces, and commits all changes to the branch. Returns `docs_committed` as its terminal success state. After this state the coordinare transitions the card to "In Review". The tech writer uses the same base performer image — no runtime execution required, only source file analysis and editing.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `httpx` (existing), `pydantic>=2.9` (existing) — **no new dependencies**
**Storage**: None (documentation committed to feature branch via `commit_file()` from 020)
**Testing**: pytest (existing in `agent/performer/tests/`)
**Target Platform**: Linux container (base performer image is sufficient — documentation is static analysis and text generation)
**Performance Goals**: Documentation pass subject to `AGENT_TIMEOUT`; `docs_committed` response within one status poll
**Constraints**: Creates CHANGELOG if absent; overwrites docs on re-run (idempotent); returns `docs_committed` with empty `files_modified` if diff has no documentable changes (never blocks on empty output)
**Scale/Scope**: ~3 modified files in performer package, ~10 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `docs_committed` is a clean terminal state; no role-specific logic leaks |
| II. Testing Discipline | PASS | Tests required for docs-committed path, empty-diff path, CHANGELOG-creation path |
| III. User Experience | N/A | Internal performer |
| IV. Performance by Design | PASS | No external API calls beyond existing git operations |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers |

## Project Structure

### Source Code (files changed)

```text
agent/performer/src/performer/
├── main.py          # MODIFIED — docs_committed terminal state; handle_docs_commit()
├── models.py        # MODIFIED — docs_committed status; docs_files_modified field on Performance
└── git.py           # USES commit_file() from 020 — commit documentation files

agent/performer/tests/unit/
└── test_main.py     # MODIFIED — tech writer path tests
```

## Detailed Implementation Plan

### Step 1 — New terminal state: `docs_committed` (`models.py`)

```python
# Add to PerformerStatus literal
"docs_committed"

# Add to Performance
docs_files_modified: list[str] = []   # paths of files written/updated
```

### Step 2 — Tech writer logic in `handle_status()` (`main.py`)

When `perf.role == "documenting"` and backend returns `done`:

```python
docs_output = backend_status.output  # AI-generated docs report

files = docs_output.get("files", [])  # list of {path, content}

for doc_file in files:
    await git.commit_file(
        perf.stand,
        doc_file["path"],
        doc_file["content"],
        f"docs: update {doc_file['path']}"
    )
    perf.docs_files_modified.append(doc_file["path"])

perf.state = "docs_committed"
return PerformerResponse(
    status="docs_committed",
    session_id=perf.session_id,
    files_modified=perf.docs_files_modified,
)
```

Note: `files` may be empty (e.g., diff has only configuration changes). The performer returns `docs_committed` regardless — it never blocks on empty output.

### Step 3 — Convention detection (AI backend responsibility)

The tech writer AI backend is responsible for:
1. Reading existing CHANGELOG, README, and docstring examples to detect style conventions
2. Producing CHANGELOG entry in the detected format (or Keep a Changelog default)
3. Producing docstrings in the detected style (or NumPy/JSDoc default)
4. Creating CHANGELOG if absent

The performer itself does not validate or reformat the AI-generated documentation — it commits it verbatim. Convention accuracy is a persona-instructions concern (see 018).

### Step 4 — Coordinare transition after `docs_committed`

In `monitor_performer` (019), `docs_committed` is the final terminal success state in the lifecycle sequence. After receiving it, `_advance_stage()` finds no next role and sets `phase = "monitoring_pr"`, transitioning the card to "In Review". No additional coordinare-side code is needed for this — it falls out of the generic lifecycle advancement logic.

## Complexity Tracking

No constitution violations. Simplest of all the performer role implementations — pure text generation and git commit, no GitHub API calls, no cycle counting.
