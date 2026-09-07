# Research: Security Workflow with the Scan in the Performer and Code-Verified Findings

All decisions below were resolved by reading the code on branch `170-security-workflow` (based on main `2d72d51`, which contains specs 164 to 169).

## R-a. Where the scan runs and how it fails closed

**Decision**: the workflow's scan step runs semgrep and bandit itself, through an injectable async runner that captures stdout and stderr separately with a per-tool timeout (default 120 seconds, `SECURITY_SCAN_TIMEOUT_S`). It does not use `Toolkit.run_command`: `adapter._command_runner` concatenates stderr onto stdout (adapter.py:66) and `performer.workspace.run_command` truncates both to 2000 characters (workspace.py:1488), either of which breaks JSON parsing of the tools' output. A missing binary (`FileNotFoundError`), a timeout, empty stdout, or unparseable stdout raises `ScannerUnavailable(tool, reason)`; the run ends `env_blocked` naming the tool before any model call. A non-zero exit with parseable JSON is a scan with findings, as in coordinare's `security_scanner._invoke`. The runner records each tool's command, exit code, finding count and duration on the record and increments `metrics.commands_run`.

**Rationale**: the fail-closed guarantee is the security control; it must not depend on output truncation limits designed for test logs.

**Alternatives**: routing the tools through `Toolkit.run_command` with `--output` files and a second `cat` (still truncated); a synthetic critical finding with `routing=halt` as 083 does (coordinare's halt path stays for the prose path, but under the workflow a hold is the established shape for "the environment, not the code").

## R-b. The security role receives the diff under the workflow

**Decision**: `_DIFF_REVIEW_ROLES` (dispatch_performer.py:349) excludes `security` because the 083 floor fetched the diff itself. Under the workflow the floor is skipped, so dispatch injects `pr_diff` for the security role exactly as for the reviewer (the same `_fetch_pr_data` and `_sanitize_pr_diff`, at most one `get_pr_diff` per dispatch). Without the workflow nothing changes. Recorded in the dispatch-payload contract.

**Rationale**: the workflow's intake, survey coverage and anchor rules all key on the parsed diff.

## R-c. Anchor rule widened for surveyed unchanged files

**Decision**: `anchor_ok_security(finding, changed_files, surveyed_files, diff_lines, survey_lines)` = (`path` in changed files AND (line in a hunk OR file opened)) OR (`path` in surveyed files) AND `introduced_by` in changed files AND evidence matches a diff or survey line. The reviewer's `anchor_in_hunks`, `anchor_in_surveyed` and `evidence_matches` are imported; only the outer combination differs. Surveyed files are the paths admitted survey commands named as whole arguments (`reviewer.survey.command_names_path`), which for an unchanged file means the survey opened it.

**Rationale**: a taint path crosses files; the sink belongs where it lives, and `introduced_by` keeps the finding tied to the PR.

## R-d. Severity and routing by code

**Decision**: `CATEGORY_TABLE = {hardcoded_secret: critical, injection: high, broken_authorization: high, insecure_deserialization: high, path_traversal: high, ssrf: high, weak_crypto: medium, missing_hardening: medium, information_leak: medium, other_insecure_pattern: medium}`; `ROUTING = {broken_authorization: architect}` with `implementer` as the default. `apply_downgrade` lowers a model finding in a blocking category to medium when `downgrade_reason` is non-empty, sets `downgraded=True`, and never touches a scanner finding. Blocking means severity in `{critical, high}`, the same set coordinare's routing block and the prose path use.

**Rationale**: the verdict must not ride on the model's severity judgement; a false positive still has a recorded escape hatch.

## R-e. Scanner findings are rule findings

**Decision**: each scanner finding becomes a `SecurityFinding` with `origin="rule"`, `tool` in `{semgrep, bandit}`, `evidence=""`, `problem=description`, `category` mapped from the tool's category (CWE number or rule id) onto the security set where a mapping exists (`89, 78, 79, 943, 94` to injection; `798` to hardcoded_secret; `502` to insecure_deserialization; `22` to path_traversal; `918` to ssrf; `285, 639, 287, 306` to broken_authorization; `327` to weak_crypto; `200` to information_leak; anything else to other_insecure_pattern) while the tool's severity is kept. The anchor rule is not applied to them. Duplicates against model findings on `(file, line, category)` collapse to the scanner finding.

**Rationale**: the tool has a real line; dropping it for missing evidence would weaken the floor. Keeping the tool's severity keeps 083's floor intact.

## R-f. Coordinare gating, both ways

**Decision**: dispatch resolves the role's workflow once (`config.performers.resolved_role(role).workflow`, the same lookup that puts `workflow` on the card_context at line 1583) before the 083 floor site; when it is `security` the floor is skipped, `state["scanner_findings"]` is set to `[]`, and `pr_diff` is injected (R-b). monitor_performer skips the 083 floor merge when `status["report"]["security"]` is a dict, because the workflow already applied the floor inside the record; a prose response has no such key and the merge runs unchanged. Tests cover both branches in both nodes.

**Rationale**: dispatch has to decide before the performer runs; the monitor can see what actually ran.

## R-g. The lift into `review_findings` and the 022 mapping

**Decision**: main.py maps `report["security"]` onto the existing statuses: `security_failed` when any finding is blocking, with `findings` in the 022 shape (`severity`, `category`, `description` = problem plus why blocking plus evidence, `file`, `line`, `routing`) so coordinare's `security_failed` block routes to architect or implementer unchanged; `security_passed` when none; `env_blocked` with the reason for a hold. `SECURITY_MAX_CYCLES` applies as in the prose path. In monitor_performer's `security_failed` block, when the report carries a security record and the target stage is implementing, the blocking implementer-routed findings are lifted into `state["review_findings"]` as a record with `changed_files`, `diff_truncated`, `verdict="changes_requested"`, `covered_files` and `findings` (path, line, category, problem, why_blocking, evidence, origin), which passes `_lift_review_findings`'s validation and `repair_plan`'s grouping. `reset_review_findings_for_reviewer` also clears on a security dispatch so a stale record never reaches a later implementing dispatch.

**Rationale**: one carrier, one repair lane, no schema change.

## R-h. Semgrep configuration and network

**Decision**: the semgrep config string is `SECURITY_SEMGREP_CONFIG` from the role's `workflow_env`, default `auto`, the value coordinare's floor used; bandit runs `-f json -r <files>`. `auto` fetches rules from the registry, so a container without egress fails the scan and the round holds; an operator may point the env at `p/default` or a local rules directory. Documented in config.example.yaml and the onboarding subsection.

**Rationale**: fail closed on a registry the container cannot reach is the correct outcome; the env gives an offline path.
