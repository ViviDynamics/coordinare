# Implementation Plan: QA Performer

**Branch**: `023-qa-performer` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Extend the existing performer codebase to support the `qa` role. The QA performer runs the existing test suite, writes new tests to cover uncovered acceptance criteria, commits any new tests to the branch, and returns `qa_passed` (all criteria satisfied) or `qa_failed` (structured failure report routed to the implementer). Environment failures (missing runtime, missing env var) return `blocked` rather than `qa_failed`. The QA performer requires the full performer image (language runtimes) rather than the base image.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `httpx` (existing), `pydantic>=2.9` (existing), `psutil` (existing) — **no new dependencies** in the performer layer; language runtimes are provided by the full image
**Storage**: None (new tests committed to feature branch via git; same `commit_file()` helper as 020)
**Testing**: pytest (existing in `agent/performer/tests/`)
**Target Platform**: Linux container — **must use full performer image** (not base); requires Node.js, Python 3.12, and standard build tools
**Performance Goals**: QA session subject to `AGENT_TIMEOUT` (default 30 min); `qa_passed`/`qa_failed` response within one status poll
**Constraints**: `QA_MAX_CYCLES` env var (default: 3); environment failures always `blocked`, never `qa_failed`; no production database or live external service access
**Scale/Scope**: ~4 modified files in performer package, ~1 new model, ~15 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `qa_passed`/`qa_failed` are clean states; `AcceptanceCriterionFailure` model is explicit |
| II. Testing Discipline | PASS | Tests required for pass path, fail-with-routing, environment-blocked path, max-cycle limit |
| III. User Experience | N/A | Internal performer |
| IV. Performance by Design | PASS | QA runs locally in container; no external I/O overhead added by the performer protocol layer |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers |

## Project Structure

### Source Code (files changed)

```text
agent/performer/src/performer/
├── main.py          # MODIFIED — qa_passed/qa_failed states; environment failure detection; QA_MAX_CYCLES
├── models.py        # MODIFIED — qa_passed, qa_failed statuses; AcceptanceCriterionFailure model; qa_cycle
├── git.py           # USES commit_file() from 020 — commit new test files
└── config.py        # MODIFIED — add QA_MAX_CYCLES setting

agent/performer/tests/unit/
└── test_main.py     # MODIFIED — QA performer path tests
```

## Detailed Implementation Plan

### Step 1 — QA failure model (`models.py`)

```python
class AcceptanceCriterionFailure(BaseModel):
    criterion: str    # text of the failing acceptance criterion
    expected: str     # what the criterion requires
    actual: str       # what the system actually did
    test: str         # test name or description that exposed the failure

class Performance(BaseModel):
    ...
    qa_failures: list[AcceptanceCriterionFailure] = []
    qa_new_tests: list[str] = []   # paths of new test files committed to branch
    qa_cycle: int = 0
```

Add `"qa_passed"` and `"qa_failed"` to `PerformerStatus`.

**`config.py`**: Add `QA_MAX_CYCLES: int = 3`.

### Step 2 — QA logic in `handle_status()` (`main.py`)

When `perf.role == "qa"` and backend returns `done`:

```python
qa_output = backend_status.output  # AI-generated QA report

# Commit any new test files written by the backend
new_tests = qa_output.get("new_test_files", [])  # list of {path, content}
for test_file in new_tests:
    await git.commit_file(perf.stand, test_file["path"], test_file["content"],
                          "test: add QA acceptance criterion tests")
    perf.qa_new_tests.append(test_file["path"])

failures = [AcceptanceCriterionFailure(**f) for f in qa_output.get("failures", [])]

if not failures:
    perf.state = "qa_passed"
    return PerformerResponse(
        status="qa_passed",
        session_id=perf.session_id,
        report={
            "criteria_checked": qa_output.get("criteria_checked", 0),
            "criteria_passed": qa_output.get("criteria_passed", 0),
            "new_tests_added": len(perf.qa_new_tests),
        }
    )

perf.qa_cycle += 1
if perf.qa_cycle >= settings.QA_MAX_CYCLES:
    perf.state = "blocked"
    perf.open_questions = [f"QA: {len(failures)} acceptance criterion failure(s) after {perf.qa_cycle} fix attempt(s)"]
    return PerformerResponse(status="blocked", ...)

perf.qa_failures = failures
perf.state = "qa_failed"
return PerformerResponse(
    status="qa_failed",
    session_id=perf.session_id,
    failures=[f.model_dump() for f in failures],
)
```

### Step 3 — Environment failure detection

If the backend signals an environment problem (missing runtime, missing env var) rather than a test failure, the performer returns `blocked`:

```python
if qa_output.get("environment_error"):
    perf.state = "blocked"
    perf.open_questions = [qa_output["environment_error"]]
    return PerformerResponse(status="blocked", session_id=perf.session_id, questions=perf.open_questions)
```

The AI backend is instructed (via persona instructions) to include `environment_error` in its output JSON when it cannot start the application due to missing infrastructure.

### Step 4 — Full image requirement

The QA performer's Dockerfile (or `config.yaml` transport config) MUST specify `coordinare-performer:full` or a custom image containing the required language runtimes. The base image is insufficient for runtime test execution.

Document this in the performer README as a QA role requirement.

## Complexity Tracking

No constitution violations.
