# Data Model: Reviewer Workflow with Verified Findings and a Structured Hand-off

## ChangedFile

Represents a file in the PR diff with hunks and surveyed state.

```python
class ChangedFile(BaseModel):
    path: str                     # File path from diff header
    hunks: list["Hunk"]          # Parsed hunks from the diff
    fully_in_diff: bool          # True if entire file is in injected text
    opened_by_survey: bool = False  # True if survey ran a command on this file
```

## Hunk

A unified diff hunk with new-side line numbers for anchor validation.

```python
class Hunk(BaseModel):
    header: str  # e.g., "@@ -10,5 +10,7 @@"
    start_line: int      # First new-side line (c in +c,d)
    end_line: int        # Last new-side line (d in +c,d)
    lines: list[str]     # Hunk content for evidence matching
```

## Finding

A code issue identified by the reviewer. The model returns a list of these.

```python
class Finding(BaseModel):
    path: str                    # Changed file path; "" if unanchored prior comment
    line: int                    # New-side line number; 0 if unanchored
    category: str                # One of the fixed set: logic_error, test_missing, style, performance, security, documentation_by_implementer, unaddressed_feedback
    problem: str                 # What is wrong
    why_blocking: str            # Why this must be fixed
    evidence: str                # Offending line or code snippet from diff/survey
    origin: Literal["model", "rule"]  # "model" from findings call, "rule" from gate
```

Finding categories are:
- `logic_error`: code logic is incorrect
- `test_missing`: test coverage lacking
- `style`: style or convention violation
- `performance`: performance issue (not an error)
- `security`: security vulnerability
- `documentation_by_implementer`: spec-165 brief present + diff touches docs/
- `unaddressed_feedback`: prior comment never dispositioned

## Disposition

How a prior comment was handled.

```python
class Disposition(BaseModel):
    prior_comment_id: str        # ID of the comment from relay_feedback
    status: Literal["fixed", "not_fixed"]  # Whether the issue was addressed
    linked_finding: Finding | None  # When not_fixed, the new finding about it
```

## ReviewRecord

The complete workflow execution record, travels in PerformerResponse.report.

```python
class ReviewRecord(BaseModel):
    changed_files: list[ChangedFile]  # Parsed diff structure
    diff_truncated: bool              # True if injected diff was capped
    unread_files: list[str] = []      # After coverage pass, still unread
    
    survey_commands: list[dict] = []  # Commands run (each: {"command": str, "output": str | None})
    survey_refusals: list[dict] = []  # Refused commands (each: {"command": str, "reason": str})
    
    findings_before_gate: list[Finding] = []  # Model's raw findings
    findings_dropped: list[Finding] = []      # Unanchored findings, dropped
    findings_after_anchor_recheck: list[Finding] = []  # Reprompted findings
    
    dispositions: list[Disposition] = []  # Prior comments handled
    
    coverage_pass_ran: bool = False     # True if coverage pass was needed
    coverage_pass_output: str = ""      # Model's coverage report
    
    verdict: Literal["approved", "changes_requested", "env_blocked"]
    - approved: no findings, full coverage, prior comments handled
    - changes_requested: findings survive after anchor check
    - env_blocked: diff fetch failed, post failed, or files still unread after coverage
    
    covered_files: set[str]  # Changed files that were in diff or surveyed
    post_error: str | None = None  # If post failed, the error message
    posted_review_url: str | None = None  # GitHub review URL if successful
```

## PersistedSession.review_findings

Stored on the card in `state_store.py` (schema v19+), cleared on reviewer re-dispatch.

```python
review_findings: dict[str, Any] | None = None
# Contains the full ReviewRecord from a changes_requested review, serialized as a plain dict
# Schema v19 adds this field; v18 and earlier load with None default
# Cleared when the reviewer is dispatched (reset_review_findings_for_reviewer)
# Injected into implementer dispatch only
```

## Score (performer/models.py)

New fields added to Score to carry inputs and outputs.

```python
class Score(BaseModel):
    ...existing fields...
    
    # Injected by coordinare (dispatch_performer.py inject_review_findings)
    review_findings: dict[str, Any] | None = None  # ReviewRecord, implementing-only
    
    # Injected by coordinare for comparison with findings
```

## Workflow state machine

The reviewer workflow transitions through states, all advanced by code (not the model):

1. **intake**: Parse diff into ChangedFile/Hunk structures, normalize relay_feedback
2. **survey**: Run read-only commands under budget, track which files were opened
3. **findings**: Call model once with schema guard, get list of Finding + Disposition
4. **gate**: Validate anchors, add unaddressed_feedback findings, check coverage
5. **post**: Post GitHub review with inline comments, handle API errors
6. **report**: Assemble ReviewRecord and PerformerResponse

On transition to **changes_requested**: ReviewRecord is returned as report["review"]; coordinare lifts it into state["review_findings"]
On transition to **approved**: ReviewRecord is returned but no lift (no implementation follows)
On transition to **env_blocked**: ReviewRecord is returned but no review is posted; nothing lifted

## Dispatch payload (Score fields)

Fields injected by dispatch_performer.py for the reviewing stage:

```
pr_diff: str                         # Sanitized diff (injected by dispatch_performer)
relay_feedback: list[dict]           # Prior comments: [{"id": str, "path": str, "line": int, "body": str}, ...]
implementation_brief: dict | None    # When present, triggers documentation_by_implementer rule
```

Fields injected for the implementing stage when review_findings are present:

```
review_findings: dict                # Complete ReviewRecord from the reviewer
```

## Personas

REVIEW persona (for findings call):
- Instructs model to read the diff, survey code, identify issues
- Lists the fixed finding categories and asks for path, line, category, problem, why_blocking, evidence
- Explains that schema violations cause reprompt
- Names the prior comments (if any) and asks for dispositions (fixed or not_fixed with new finding if not_fixed)
- Notes that the verdict will be determined by code (no verdict field in output)
- Instructs to limit findings to 30 (FR-005)

SURVEY_COVERAGE persona (for coverage pass):
- Instructs model to run one more survey turn
- Names the files that have not been opened
- Asks to open each and report whether they appear to address the review's concerns

## Mutation test rules (pure functions in gate.py)

Every gate function must fail under a specific mutation:

1. **anchor_in_hunks(finding, changed_files, hunks_by_path)**: Returns True if finding.path is in changed_files AND finding.line is in a hunk's range. Mutation: remove the changed_files check.
2. **anchor_in_surveyed(finding, surveyed_files)**: Returns True if finding.path is in surveyed_files. Mutation: always return True.
3. **evidence_matches(finding, diff_text, surveyed_output)**: Returns True if finding.evidence appears in diff_text or surveyed_output. Mutation: always return True.
4. **has_disposition(prior_comment, dispositions_list)**: Returns True if the comment ID is in dispositions. Mutation: always return True.
5. **full_coverage(changed_files, covered_files)**: Returns True if every changed file is in covered_files. Mutation: always return True.

## Rule predicates (the exact operands the gate tests mutate)

- `anchor_ok(finding, changed_files, surveyed_files, diff_lines, survey_output_lines)`:
  `finding.path in {f.path for f in changed_files}` AND (`finding.line` within one of that file's new-side hunk ranges OR `finding.path in surveyed_files`) AND (`finding.evidence == ""` for rule-origin findings, else `finding.evidence` is a substring of some line in the diff or in the survey output for that file).
- `full_coverage(changed_files, surveyed_files, truncated)`: every changed file is `fully_present` in the injected diff or is in `surveyed_files`; when `truncated` is true, the coverage pass must have run (recorded on the report) before this predicate is evaluated for approval.
- `verdict(findings)`: `changes_requested` when `len(findings) > 0`, else `approved`. Approval additionally requires `full_coverage`, else the run ends as `env_blocked` naming the unread files.
- `documentation_tree`: a changed path counts as documentation when it starts with `docs/` or `doc/`, or its basename starts with `README`, `CONTRIBUTING` or `CHANGELOG` (the same set spec 167's scope rule uses).
- `brief_present(score)`: `score.implementation_brief` is a non-empty dict.
- `Finding.evidence`: at most 200 characters; empty only when `origin == "rule"`.
- Prior comments without a path or line normalise to `path=""`, `line=0`, `origin="rule"` when they become `unaddressed_feedback` findings; the post step maps that anchor to the PR body (no inline comment) and names the comment id.

## The canonical flow of findings

1. The workflow's report is `{"review": ReviewRecord, "workflow_metrics": {...}}`; `ReviewRecord.findings` is the list of surviving findings.
2. `main.py` returns `PerformerResponse(status="changes_requested" | "approved", report=<that dict>)`; the workflow has already posted the GitHub review, so the prose post is skipped (FR-015 keeps the prose path unchanged when the workflow is off).
3. `monitor_performer` lifts `report["review"]["findings"]` into `state["review_findings"]` on `changes_requested` from the reviewing stage only.
4. `dispatch_performer` clears `state["review_findings"]` when the reviewer is dispatched and injects it as `card_context["review_findings"]` for the implementing stage only; `Score.review_findings` declares the field.
5. The spec-167 plan selects the `repair` lane when `score.review_findings` is non-empty.

## Persona placeholders

| Kind | Placeholders |
| --- | --- |
| SURVEY | `{changed_files}`, `{diff_excerpt}`, `{command_budget}` |
| COVERAGE | `{unread_files}` |
| FINDINGS | `{diff}`, `{survey_notes}`, `{prior_comments}`, `{categories}`, `{brief_summary}` |
| REANCHOR | `{dropped_findings}`, `{changed_files}` |
| REPAIR_REVIEW (spec 167) | `{findings}`, `{path}` |
