# Research: Reviewer Workflow with Verified Findings and a Structured Hand-off

All decisions below were resolved by reading the code on branch `169-reviewer-workflow` (based on main `78af1f4` which contains specs 164-167) and comparing against the spec-164, 165, 166, and 167 patterns.

## R-a. Diff parser and truncation handling

**Decision**: The `_sanitize_pr_diff` function in dispatch_performer.py (line 348-390) splits the diff by `^diff --git ` regex, filters vendor/noise/binary files, truncates to `_DIFF_INJECT_MAX_CHARS = 60_000`, and appends a `[coordinare: omitted X tooling and Y binary files; diff truncated to 60000 chars , run gh pr diff for full changes]` note when anything is dropped. Hunk headers (`@@ -a,b +c,d @@`) survive; they are part of each section and not dropped by the sanitizer.

Truncation is marked in the text (line 382-383), not as a separate field. The reviewer intake must parse this note from the filtered text and record `truncated = True` as a finding.

A separate `get_pr_diff` call on line 422 returns `(raw_diff, changed_files)` as a tuple; the reviewer workflow receives the sanitized diff in `Score.pr_diff` and the list of changed file paths as a separate field on Score (to be registered in the dispatch contract).

**Rationale**: The sanitizer is the single source of truth for truncation. Hunk structure is needed to recover line ranges for findings (spec FR-006). The list of changed files is injected as a separate Score field so the intake can validate findings against it.

**Alternatives Considered**:
- Detect truncation only in the intake by searching for the note. Rejected: the marker could appear in actual code. The note is reliable; rely on it.
- Store truncation as a separate boolean field on Score. Rejected: one extra field clutters the contract; parsing the note in intake is sufficient.

## R-b. Finding anchor rule (files and line ranges)

**Decision**: A finding is valid (anchored) only if:
1. Its `path` is in the `changed_files` list returned by `get_pr_diff`, AND
2. Its `line` falls within a hunk's new-side line range from the parsed diff (e.g., hunk `@@ -a,b +c,d @@` covers lines c to d), OR
3. The file was opened by the survey (recorded in the intake's `surveyed_files` set).

If a finding's path is in `changed_files` but its line is outside all hunks in the diff AND the file was not opened by survey, it is unanchored and dropped (recorded as dropped).

A finding at a deleted line (outside the hunks but the file was in the diff) is treated as unanchored per the above rule; when re-anchoring, the model is told "this line no longer exists" and must move to the hunk header line or an adjacent hunk.

**Rationale**: The diff shows only changed lines; lines outside hunks are not visible to the reviewer. If the survey opened a file, the model had a chance to read it and the line could be valid there. This ensures every posted finding points at a line the human can see in GitHub's review UI.

**Alternatives Considered**:
- Accept any line in the changed file regardless of hunks. Rejected: GitHub review UI cannot place a comment on a line outside the diff; it will silently drop it or fail.
- Require ALL findings to be in hunks. Rejected: the survey opens files; a finding from surveyed output must be valid.

## R-c. Prior comments without path or line

**Decision**: When `relay_feedback` (from coordinare, spec 128/126) contains a comment with no `path` or `line` field, the intake normalizes it as `{"id": str, "path": "", "line": 0, "body": str}` and marks it as "unanchored prior comment". On the gate step, any prior comment without a disposition is added as a finding with:
- `path: ""` (empty string, special marker for unanchored)
- `line: 0`
- `category: "unaddressed_feedback"`
- `problem: f"Earlier comment left unaddressed: {comment['body'][:100]}"`
- `evidence: ""` (no code evidence to cite)
- `origin: "rule"` (added by code, not the model)

In the gate, a finding with `path: ""` is NOT subject to the anchor rule (it is known to be unanchored and that is intentional). It is posted to GitHub with `path: "/"` and `line: 1` as a fallback, so the human sees it somewhere in the review (GitHub rejects inline comments without a valid path/line).

**Rationale**: A prior comment that was never dispositioned is as actionable as any other finding; the gate must ensure it is not lost. Anchoring it to the empty path/line 0 is a marker that it needs special handling on post.

**Alternatives Considered**:
- Drop undispositioned prior comments silently. Rejected: spec FR-007 explicitly requires a finding for every undispositioned comment.
- Anchor them to the first hunk line. Rejected: they may not relate to that file at all; the human should see the comment body as proof the reviewer saw it.

## R-d. Posting to GitHub and mapping event

**Decision**: The `post_pull_request_review` function in performer/github.py (line 236-298) takes:
- `event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]`
- `body: str` (top-level review text)
- `comments: list[dict]` where each dict has `path`, `line`, `body`

The reviewing stage (main.py) currently posts with `event="COMMENT"` always (line 2253). Under the workflow path, map the verdict:
- If `findings` survive (non-empty list): `event="REQUEST_CHANGES"`, `body` names fixed prior comments ("Addresses earlier feedback on [comment IDs]"), `comments` are one per finding with the finding's `path`, `line`, and `body`.
- If no findings and full coverage: `event="COMMENT"`, `body` says "Automated review found no issues", `comments` empty.
- If no findings but missing coverage: run ends as environment hold, nothing posted.

For findings with `path: ""`, post with `path: "/"` and `line: 1` as a fallback since GitHub rejects comments on a non-existent file.

**Rationale**: REQUEST_CHANGES signals a real issue exists. COMMENT is for informational passes. The event gates whether the performer reports `changes_requested` or `approved`.

**Alternatives Considered**:
- Always use COMMENT, let the human approve. Rejected: spec FR-010 requires REQUEST_CHANGES when findings exist.
- Use APPROVE when findings are fixed. Rejected: spec 13-non-goal says "Approving on behalf of a human"; only COMMENT or REQUEST_CHANGES allowed.

## R-e. Repair lane grouping and milestones

**Decision**: When `review_findings` are present on the implementer's Score (injected from the lifted findings), the plan step's `select_lane` function returns `"repair"` as a new lane value. The plan builds milestones by grouping findings by `path`:
- One `MilestonePlan` per unique `path` in the findings, with `goal: f"address review findings in {path}"`
- The milestone's `brief` carries all findings for that path verbatim, plus a REPAIR_REVIEW persona that lists them
- Milestones run in sorted path order
- Each milestone follows the existing "chore" pattern: one implementation turn, baseline check, green check, commit

After all milestones are green, quality and CI gates run as normal.

**Rationale**: Findings group naturally by file. Grouping avoids duplicating findings across milestones and keeps the goal focused.

**Alternatives Considered**:
- One milestone per finding. Rejected: many findings in one file would create many small milestones; grouping is cleaner.
- Flatten all findings into one milestone. Rejected: a large PR with many file changes would be one unwieldy milestone.

## R-f. Schema version 19 and backward compatibility

**Decision**: `state_store.py` (line 25) bumps `CURRENT_SCHEMA_VERSION` from 18 to 19. `PersistedSession` gains a field `review_findings: dict[str, Any] | None = None` with a default of None. The findings dict stores the complete ReviewRecord (data-model.md) as a plain dict for JSON portability. Older snapshots (v18 and earlier) load unchanged; the review_findings field defaults to None.

Migration is zero-line: the field validator allows None and any dict, with a post-load cleanup that drops malformed findings (the same pattern as `_drop_corrupt_assessment`).

On reviewer dispatch (dispatch_performer.py `reset_review_findings_for_reviewer` function), any prior review_findings on the card are cleared (`state["review_findings"] = None`), ensuring a failed reviewer round does not leave stale findings for the implementer.

On reviewer completion with changed_requested (monitor_performer.py), the report's `review_findings` key is lifted into `state["review_findings"]` for persistence.

On implementer dispatch (dispatch_performer.py `inject_review_findings` function), review_findings are injected into the implementing stage payload only, under the key `review_findings`, as a complete dict. Other stages never receive it.

**Rationale**: Findings must survive daemon restart (spec FR-012). The schema versioning pattern is established (every spec with persisted state bumps the version and provides backward-compatible defaults). Clearing on dispatch mirrors blueprint/assessment and avoids stale state. Injecting only at implementing stage keeps other roles unaffected.

**Alternatives Considered**:
- Store findings in the report only, not persisted. Rejected: daemon restart mid-round loses them and the implementer has no repair context.
- Add review_findings to all stage payloads. Rejected: only the implementer uses them; others should not see them per spec.

## R-g. Who posts, and why FR-015 holds

**Decision**: the workflow's post step posts the GitHub review itself (`REQUEST_CHANGES` with inline comments, or `COMMENT` when there are no findings) and `main.py`'s reviewing branch, on seeing the `review` report key, only maps the verdict and skips the prose post. The prose path keeps posting `COMMENT` for every verdict exactly as today (it never approves on the PR either); nothing in it becomes conditional on the workflow name.

**Rationale**: FR-010 wants inline comments on the findings' lines, which only the workflow has anchored; FR-015 wants the prose path byte for byte unchanged. Posting from the workflow satisfies both without a branch in the prose code. `REQUEST_CHANGES` is not an approval, so the review design (only humans approve) is untouched.

**Alternatives considered**: posting from main.py with a workflow-name condition (rejected: it edits the prose path); posting `COMMENT` from the workflow too (rejected: findings would not block the PR in GitHub's own UI, which is the signal a human sees).
