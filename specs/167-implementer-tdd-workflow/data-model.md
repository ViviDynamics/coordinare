# Data Model: Implementer Test-First Workflow with a Verified Hand-off

All schema definitions for the workflow state, turn coordination, and reporting.

## TurnBrief

Input to one harness invocation. Passed to the agent via persona instructions and a structured context block.

```python
@dataclass
class TurnBrief:
    """What one harness turn is asked to do (FR-002)."""
    
    kind: Literal["tests", "implement", "repair"]
    milestone_goal: str  # the goal for this milestone, e.g. "User can sign in with email"
    scope: str  # scope block, e.g. "auth/ and the user login flow in app/"
    done_when: str  # acceptance criterion for the milestone, exact copy from brief
    forbidden_paths: list[str]  # e.g. ["docs/", "src/admin/"] for a tests turn
    failing_output: str | None  # raw test failure output for repair turns, None otherwise
    milestone_index: int  # 0-indexed position in the plan (for metrics and logs)
```

## TurnResult

Output from one harness invocation. Captured by the performer and processed by the workflow.

```python
@dataclass
class TurnResult:
    """Exit state and artifacts from one harness turn (FR-002)."""
    
    exit_state: Literal["success", "timeout", "failed"]
    output_tail: str  # last 2000 characters of harness output for debugging
    files_changed: dict[str, Literal["added", "modified", "deleted"]]  # path -> change kind
    wall_time_ms: int  # elapsed time from turn start to end
    harness_commits: list[str]  # SHAs of commits the harness made (to be squashed)
```

## Baseline

Test state before the first turn, carried forward through the workflow.

```python
@dataclass
class Baseline:
    """Passing test set before any milestone work (FR-004)."""
    
    # One of these two is non-empty:
    test_names: list[str]  # exact test identifiers when the runner supports --collect-only
    pass_count: int  # fallback: number of passing tests when names are unavailable
    fail_count: int  # fallback: number of failing tests at baseline
    
    # Metadata:
    stack: str  # "python", "ruby", "node", "make", or "unknown"
    detected_from: str  # "pyproject.toml", "Gemfile", etc.
```

Validation: exactly one of `test_names` or `pass_count` is set (a model validator); `test_names` when the runner lists tests, counts otherwise.

## MilestonePlan

Milestones derived from the implementation brief or card.

```python
@dataclass
class MilestonePlan:
    """One milestone in the workflow (FR-003)."""
    
    index: int  # 0, 1, 2, ...
    goal: str  # short goal text, at most 256 characters
    scope: str  # scope block from the brief
    done_when: str  # acceptance criterion
    
    @classmethod
    def from_brief(cls, brief: dict) -> list["MilestonePlan"]:
        """Derive milestones from the architect's implementation brief.
        
        When brief is None or empty, returns one milestone per the card's
        acceptance criteria (FR-003 fallback).
        """
        # if brief exists and has milestones, return those
        # else return one milestone with goal="Implement acceptance criteria"
```

## PerTurnAttempt

One attempt at a turn, recorded in the run record.

```python
@dataclass
class PerTurnAttempt:
    """One run of a turn step (e.g. first implementation attempt, or a repair)."""
    
    kind: Literal["tests", "implement", "repair"]
    milestone_index: int
    attempt_number: int  # 1, 2, 3 for implementation; 1, 2 for repair, etc.
    exit_state: Literal["success", "timeout", "failed"]
    wall_time_ms: int
    files_changed: int  # count
    has_out_of_scope_reverts: bool  # True if doc or source edits were reverted
    failure_reason: str | None  # human-readable why it failed (e.g., "tests all passed without impl")
```

## PerMilestoneRecord

One milestone's full history.

```python
@dataclass
class PerMilestoneRecord:
    """Workflow record for one milestone (FR-018)."""
    
    index: int
    goal: str
    done_when: str
    
    tests_attempt: PerTurnAttempt | None  # the tests turn, if run
    tests_reprompt: PerTurnAttempt | None  # reprompt after vacuous tests
    
    implement_attempts: list[PerTurnAttempt]  # up to 3 attempts
    
    implementation_successful: bool  # tests and implementation both passed
    failure_reason: str | None  # if not successful, why (e.g., "three impl attempts exhausted")
```

## QualityAttempt

Record of one quality command or lint run.

```python
@dataclass
class QualityAttempt:
    """One pass or repair of the quality gate (FR-010)."""
    
    command: str  # e.g. "ruff check ."
    attempt_number: int  # 1 or 2
    exit_code: int
    output_tail: str  # last 500 chars of output
    wall_time_ms: int
    passed: bool
```

## CIAttempt

Record of one CI polling and repair cycle.

```python
@dataclass
class CIAttempt:
    """One attempt at the CI polling gate (FR-013)."""
    
    attempt_number: int  # 1, 2, or 3
    failing_checks: list[str]  # names of failed checks on this attempt
    repair_needed: bool  # True if a check failed (not pending, not green)
    wall_time_ms: int
    log_excerpt: str  # first 4000 chars of the first failing job's log
```

## RunRecord

Complete workflow run history, travels in the performer response report.

```python
@dataclass
class RunRecord:
    """Full history of the workflow run (FR-018)."""
    
    # Outcomes:
    status: Literal["pr_opened", "changes_requested", "partial_progress", "env_blocked"]
    reason: str  # human-readable outcome reason
    
    # Workflow structure:
    milestones_planned: int
    milestones_completed: int
    next_focus_milestone: str | None  # if partial_progress, the failing milestone
    
    # Per-milestone history:
    per_milestone: list[PerMilestoneRecord]
    
    # Quality and CI:
    quality_attempts: list[QualityAttempt]
    ci_attempts: list[CIAttempt]
    
    # Scope violations:
    scope_reverts: list[dict]  # [{"path": "docs/index.md", "kind": "reverted_doc", "reason": "..."}]
    
    # Timing (FR-018):
    phase_durations_ms: dict[str, int]  # "planning", "baseline", "per_milestone", "quality", "local_gate", "ci"
    total_duration_ms: int
    
    # For coordinare metrics (spec 161/166 pattern):
    turn_count: int
    model_calls: int
    github_api_calls: int
```

## State Machine

The workflow is a deterministic state machine. At each step, the state transitions are determined by code, never by model output.

### States

| State | Meaning | Can transition to | Terminal |
|-------|---------|-------------------|----------|
| `intake` | Assembling the plan from brief or card | `baseline` | No |
| `baseline` | Running tests once to record baseline | `cycle` | No |
| `cycle` | Per-milestone loop: tests, red check, impl, green check | `cycle` (next milestone) or `quality` | No |
| `quality` | Running lint + declared commands, repair loop | `local_gate` | No |
| `local_gate` | Running spec-089 local test gate | `push_and_pr` or terminal | No |
| `push_and_pr` | Pushing branch and opening PR | `ci_wait` | No |
| `ci_wait` | Polling GitHub checks until pass/fail/timeout | terminal | No |

### Transitions with Guards

- `intake` -> `baseline`: checks that `test_command is not None` (else -> `env_blocked`). Checks that `milestones` is non-empty (else -> `env_blocked`).
- `baseline` -> `cycle`: always (to the first milestone).
- `cycle` (per milestone) -> `cycle` (next): when milestone successful, continue to next.
- `cycle` (last milestone) -> `quality`: when last milestone successful.
- `cycle` (any) -> `partial_progress`: when a milestone fails (red reprompt still fails, or three impl attempts exhausted, or stuck on a vacuous test).
- `quality` -> `local_gate`: when all commands pass.
- `quality` -> `partial_progress`: when a repair exhausts (two repairs failed).
- `local_gate` -> `push_and_pr`: when gate passes.
- `local_gate` -> `changes_requested`: when gate fails (not env_blocked).
- `local_gate` -> `env_blocked`: when gate detects environment signal.
- `push_and_pr` -> `ci_wait`: always.
- `ci_wait` -> `pr_opened`: when all checks pass.
- `ci_wait` -> `partial_progress`: when a repair exhausts (three repairs failed) or no progress detected (same checks failing twice).
- `ci_wait` -> `env_blocked`: when checks stay pending past timeout budget.

### Caps (FR-004, FR-006, FR-010, FR-013, FR-016)

| Item | Cap | Guard |
|------|-----|-------|
| Tests turns per milestone | 1 + 1 reprompt | after reprompt fails -> fail milestone |
| Implementation turns per milestone | 3 | after 3 failed -> fail milestone |
| Quality repairs | 2 | after 2 failed -> partial_progress naming the command (FR-010) |
| CI repairs | 3 | after 3 failed, or no progress on two consecutive polls -> partial_progress naming the check (FR-013) |
| Harness turn wall clock | 20 minutes | per-turn timeout -> count as failed attempt |
| CI wait budget | 30 minutes | past budget -> env_blocked hold |
| Total run wall clock | 120 minutes | enforced by coordinare (actor timeout) |

---

## Terminal outcomes and the report key

The workflow's result is `PerformerResponse.report`, a dict whose top-level key `implementer_run` holds the RunRecord. That key is what `main.py`'s implementing branch tests for to take the workflow path (the way 165 tests for `blueprint` and 166 for `assessment`); its absence is the prose path, byte for byte (FR-017).

| Outcome | Status reported | Reason field | Kind |
| --- | --- | --- | --- |
| all checks green | `pr_opened` | none | hand-off |
| a milestone failed (vacuous tests twice, or three red implementation attempts) | `partial_progress` with `next_focus` = the milestone goal | last failure excerpt | code failure, bounced by coordinare |
| quality repairs exhausted | `partial_progress` | the failing command and its last output | code failure |
| CI repairs exhausted or no progress on two consecutive polls | `partial_progress` | the failing check names and the last log excerpt | code failure |
| no test runner detected before the first turn | `env_blocked` | "no test command detected" | environment hold |
| the 089 local gate's environment signal | `env_blocked` | the gate's env_reason | environment hold |
| checks pending past the wait budget | `env_blocked` | the pending check names | environment hold |
| the 089 local gate fails on tests (should not happen after green milestones) | `changes_requested` | the gate's summary | existing 089 shape, unchanged |

Counters on the RunRecord: `turn_count` is the number of PerTurnAttempt entries across all milestones and repairs; `model_calls` is `WorkflowMetrics.model_calls` (zero unless a step calls the model directly); `github_api_calls` is incremented by the CI phase for every check-runs poll, log fetch and PR call.

## Turn persona templates

Each turn kind renders one template from `workflows/implementer/personas.py` with named placeholders; the tests assert on the placeholders, not on prose:

| Kind | Placeholders | Forbidden lines (must appear verbatim) |
| --- | --- | --- |
| TESTS | `{milestone_goal}`, `{scope_paths}`, `{done_when}`, `{test_conventions}` | "Do not change source files.", "Do not create or edit documentation.", "Do not commit." |
| IMPLEMENT | `{milestone_goal}`, `{scope_paths}`, `{done_when}`, `{failing_tests}`, `{failure_excerpt}` | "Make exactly these tests pass.", "Do not create or edit documentation.", "Do not commit." |
| REPAIR_TESTS | `{milestone_goal}`, `{passing_test_files}` | "These tests pass without the behaviour; make them fail for the right reason.", plus the TESTS forbidden lines |
| REPAIR_IMPLEMENT | as IMPLEMENT | as IMPLEMENT |
| REPAIR_QUALITY | `{command}`, `{tool_output}` | "Fix only what this tool reports.", "Do not create or edit documentation.", "Do not commit." |
| REPAIR_CI | `{check_name}`, `{log_excerpt}` | "Fix what this check reports.", "Do not create or edit documentation.", "Do not commit." |

Placeholders are filled by `str.format` on a template with no other braces; a missing placeholder is a KeyError at build time, which the persona tests catch. Newlines inside values are preserved.

## Notes on Persistence

The run record is produced at the end of the workflow and travels in `PerformerResponse.report` to the coordinare. Coordinare does not persist it (the performer is one-shot, spec FR-010). The report is emitted as an event and captured by coordinare's existing event sink (performer/models.py `BackendEvent`). Coordinare's observability layer (`monitor_performer`) lifts the run record into logs and metrics.

No new coordinare state (beyond what exists in the session) is needed: the run record is not consumed by other roles and does not drive subsequent decisions. The "next focus milestone" is carried in the partial_progress block, which coordinare already handles.
