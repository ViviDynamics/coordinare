# Data Model: Security Workflow with the Scan in the Performer and Code-Verified Findings

## Reused from spec 169 (imported, unchanged)

`ChangedFile`, `Hunk` (`reviewer/models.py`), `parse_unified_diff`, `detect_truncation`, `diff_lines`, `line_in_hunks` (`reviewer/diffparse.py`), `run_reviewer_survey`, `mark_opened`, `command_names_path`, `unread_files`, `survey_output_lines` (`reviewer/survey.py`), `model_findings_schema` (`reviewer/models.py`), `anchor_in_hunks`, `anchor_in_surveyed`, `evidence_matches`, `full_coverage`, `cap_findings` (`reviewer/gate.py`), `pr_number_from_url`, `post_review` machinery (`reviewer/post.py`), `write_free_check` (`reviewer/report.py`).

## SECURITY_CATEGORIES, CATEGORY_TABLE, ROUTING

```python
SECURITY_CATEGORIES = ("injection", "broken_authorization", "hardcoded_secret", "insecure_deserialization",
                       "path_traversal", "ssrf", "weak_crypto", "missing_hardening", "information_leak", "other_insecure_pattern")
CATEGORY_TABLE: dict[str, str] = {  # category -> default severity
    "hardcoded_secret": "critical", "injection": "high", "broken_authorization": "high", "insecure_deserialization": "high",
    "path_traversal": "high", "ssrf": "high", "weak_crypto": "medium", "missing_hardening": "medium",
    "information_leak": "medium", "other_insecure_pattern": "medium"}
ROUTING: dict[str, str] = {"broken_authorization": "architect"}   # default "implementer"
BLOCKING = frozenset({"critical", "high"})
CWE_TO_CATEGORY: dict[str, str] = {"89": "injection", "78": "injection", "79": "injection", "943": "injection", "94": "injection",
    "798": "hardcoded_secret", "502": "insecure_deserialization", "22": "path_traversal", "918": "ssrf",
    "285": "broken_authorization", "639": "broken_authorization", "287": "broken_authorization", "306": "broken_authorization",
    "327": "weak_crypto", "200": "information_leak"}   # anything else -> other_insecure_pattern
```

## SecurityFinding

```python
class SecurityFinding(_Bounded):
    path: str                                   # changed or surveyed file; scanner findings: the tool's file
    line: int >= 0
    category: str (1..64)                       # one of SECURITY_CATEGORIES after mapping
    problem: str (1..500)
    why_blocking: str (1..500)
    evidence: str (<=200)                       # "" only for origin == "rule" (scanner findings)
    origin: Literal["model", "rule"]
    severity: Literal["critical", "high", "medium", "low"]
    routing: Literal["implementer", "architect"]
    introduced_by: str                          # a changed file; scanner findings: the tool's file
    tool: Literal["model", "semgrep", "bandit"]
    downgraded: bool = False
    downgrade_reason: str (<=300) = ""
```

Validator: `evidence` empty only when `origin == "rule"`; `downgraded` true only when `tool == "model"`.

## ModelSecurityFinding (the model-facing schema, built per run)

`model_findings_schema` is extended by a security builder that adds `introduced_by: str` and `downgrade_reason: str (<=300) = ""` to each finding and keeps `extra="forbid"`, so `severity`, `routing` and `verdict` keys are schema violations. `category` is the Literal of `SECURITY_CATEGORIES`; the list is capped at 30; `dispositions` is absent (no prior comments).

## ScanResult

```python
class ScanResult(_Bounded):
    tool: Literal["semgrep", "bandit"]
    command: str
    exit_code: int | None
    finding_count: int >= 0
    duration_ms: int >= 0
    error: str | None = None                    # set only on the hold path, for the record
```

## SecurityRecord

```python
class SecurityRecord(_Bounded):
    changed_files: list[ChangedFile]
    diff_truncated: bool
    scan: list[ScanResult]                      # one per tool, in run order
    unread_files: list[str] = []
    survey_commands: list[dict] = []
    survey_refusals: list[dict] = []
    findings_before_gate: list[SecurityFinding] (<=30)   # model findings after severity/routing assignment, before the anchor rule
    findings_dropped: list[SecurityFinding] (<=30)
    findings_after_anchor_recheck: list[SecurityFinding] (<=30)
    scanner_findings: list[SecurityFinding] (<=200)       # rule findings from the tools, after category mapping
    blocking: list[SecurityFinding] (<=230)               # surviving, severity in BLOCKING, after dedup (30 model + 200 tool at most)
    advisory: list[SecurityFinding] (<=230)               # surviving, medium or low
    coverage_pass_ran: bool = False
    coverage_pass_output: str = ""
    verdict: Literal["security_passed", "security_failed", "env_blocked"]
    hold_reason: str | None = None              # scanner tool and reason, unread files, or post error
    covered_files: list[str]
    post_error: str | None = None
    posted_review_url: str | None = None
    workflow_metrics: dict = {}
```

## Rule predicates (the exact operands the gate tests mutate)

- `anchor_ok_security(f, changed_files, surveyed_files, diff_lines, survey_lines)`: `(f.path in changed AND (anchor_in_hunks OR anchor_in_surveyed)) OR (f.path in surveyed)`; AND `f.introduced_by in changed`; AND `evidence_matches`. Mutations: drop the `introduced_by` clause; accept an unsurveyed unchanged path; drop the evidence clause.
- `severity_for(category)`: `CATEGORY_TABLE[category]`, `medium` for an unknown category. Mutation: return `high` for `weak_crypto`; return `medium` for `hardcoded_secret`.
- `routing_for(category)`: `ROUTING.get(category, "implementer")`. Mutation: route `broken_authorization` to implementer.
- `apply_downgrade(f)`: when `f.tool == "model"` and `f.severity in BLOCKING` and `f.downgrade_reason.strip()`: severity `medium`, `downgraded=True`. Mutations: downgrade a scanner finding; downgrade without a reason.
- `map_scanner_category(raw)`: a CWE number or a string containing one maps through `CWE_TO_CATEGORY`, else `other_insecure_pattern`. Mutation: map `798` to `other_insecure_pattern`.
- `merge_scanner_findings(model_survivors, scanner)`: scanner findings appended; a model finding with the same `(path, line, category)` as a scanner finding is removed; the scanner's severity is kept. Mutation: keep the model's severity; skip the append.
- `split_blocking(findings)`: `(blocking, advisory)` by `severity in BLOCKING`. Mutation: treat `high` as advisory.
- `verdict(blocking, coverage_ok)`: `security_failed` when blocking is non-empty; else `security_passed` if `coverage_ok` else `env_blocked`. Mutations: pass with blocking findings; pass without coverage.
- `full_coverage` is the reviewer's, unchanged.

## The canonical flow of findings

1. scan -> `scanner_findings` (rule, tool severity, mapped category). A `ScannerUnavailable` ends the run `env_blocked` with `hold_reason="<tool>: <reason>"` before any model call.
2. findings call -> model findings; `severity_for` and `routing_for` applied; `apply_downgrade`; recorded as `findings_before_gate`.
3. anchor rule -> survivors and `findings_dropped`; one re-anchor call over the dropped ones, its output gated again into `findings_after_anchor_recheck`.
4. the model's survivors capped at 30, then `merge_scanner_findings` (tool findings are never sliced out, at most 200); `split_blocking`.
5. `verdict(blocking, full_coverage(...))`.
6. post: `REQUEST_CHANGES` with inline comments for blocking findings inside a hunk of a changed file, the rest (and every advisory) in the body, when blocking is non-empty; else `COMMENT` listing advisories and downgrades. A failed post sets `verdict="env_blocked"` and `post_error`.
7. report: `{"security": SecurityRecord, "write_free_check", "workflow_metrics"}`.
8. main.py: `security_failed` with `findings` = blocking in the 022 shape (`severity`, `category`, `description` = `"{category}: {problem} Why blocking: {why_blocking} Evidence: {evidence}"`, `file`, `line`, `routing`), `security_passed`, or `env_blocked` with `hold_reason`.
9. monitor_performer: the 083 floor merge is skipped when `report["security"]` exists; on `security_failed` routed to implementing, `review_findings` receives `{changed_files, diff_truncated, verdict: "changes_requested", covered_files, findings: [blocking implementer-routed findings in the reviewer Finding shape]}`.
10. dispatch_performer: `reset_review_findings_for_reviewer` clears on `reviewing` and `security` dispatch; `inject_review_findings` unchanged (implementing only); `repair_plan` groups by path.

## Persona placeholders

| Kind | Placeholders |
| --- | --- |
| FINDINGS | `{categories}`, `{diff}`, `{scan_findings}`, `{survey_notes}`, `{brief_summary}` |
| REANCHOR | `{dropped_findings}`, `{changed_files}` |
| COVERAGE | the reviewer's `SURVEY_COVERAGE_PERSONA`, `{unread_files}` |

## Budgets (`SecurityBudgets.from_env`)

| Env | Default | Meaning |
| --- | --- | --- |
| `SECURITY_SCAN_TIMEOUT_S` | 120 | per tool |
| `SECURITY_SEMGREP_CONFIG` | `auto` | semgrep `--config` value |
| `SECURITY_SURVEY_MAX_COMMANDS` | 12 | per survey turn |
| `SECURITY_SURVEY_MAX_OUTPUT_CHARS` | 4000 | kept per command |
| `SECURITY_MAX_FINDINGS` | 30 | model findings cap, never above 30 |
