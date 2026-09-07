# Feature Specification: Security Workflow with the Scan in the Performer and Code-Verified Findings

**Feature Branch**: `170-security-workflow`
**Created**: 2026-09-07
**Status**: Draft
**Input**: User description: "Extend the spec-164 role-workflow layer to the security performer as a sibling of the spec-169 reviewer workflow: the static scan runs inside the performer and fails closed, a read-only survey with coverage, one schema-guarded findings call over a fixed security category set, a gate that verifies every anchor and assigns severity and routing by code, exactly one GitHub review, and blocking findings lifted into the implementer's repair lane. Default off via workflow: security."

## Problem

The security stage is one prose turn over the injected diff. Coordinare runs semgrep and bandit once at dispatch and forces a failure when they find something serious, but the model's own findings are free text: nothing checks that a finding points at a line the model saw, the model chooses the severity that decides the verdict, and the same model decides whether the fix belongs to the implementer or the architect. The stage commits a `security.md` report into the card's docs folder and posts one advisory comment per medium or low finding with a fingerprint to avoid repeats, which makes it the second committer on a branch it did not write. The fail-closed scanner floor lives in coordinare, so the performer's own view of the code and the scanner's view are merged after the fact, and a scanner that is missing in the container is invisible to the performer.

## Goals

- The verdict is derived by code from findings whose anchors were verified against the diff or the surveyed code, and from scanner findings that code can never drop.
- Severity and routing are functions of a fixed category table, visible in the record, with one narrow, recorded way for the model to downgrade a blocking category.
- The scan runs where the code is, inside the performer, and a scanner that is missing or broken holds the card instead of passing it.
- The stage posts exactly one review and commits nothing.
- Blocking findings reach the implementer through the same carrier the reviewer uses, so the repair lane fixes them without new plumbing.
- Without the workflow flag the stage, including coordinare's scanner floor, behaves byte for byte as today.

## Non-goals

- Dependency or container image scanning (a separate spec).
- Fixing findings; the security stage reports, the implementer or architect repairs.
- Changing which stage follows security in the lifecycle.
- Prior-comment dispositions across security rounds; each round scans and analyses fresh.
- A new persisted carrier; the blocking findings travel in the spec-169 `review_findings` record.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A vulnerable change is caught with anchored, actionable findings (Priority: P1)

An implementer's PR concatenates a request parameter into an SQL statement. The security stage scans the changed files, surveys the code around the change, reports an injection finding anchored to the offending line with the evidence copied verbatim, marks it blocking by rule, routes it to the implementer, posts one review requesting changes with an inline comment on the line, and the next implementer dispatch carries the finding into the repair lane.

**Why this priority**: this is the stage's purpose; a finding the implementer cannot locate is a bounce with nothing to fix.

**Independent Test**: run the workflow against the `injection` fixture with a stubbed model and a fake scanner; the record shows one blocking finding on the changed line, one REQUEST_CHANGES review with one inline comment, and the report main.py turns into `security_failed` with the finding in the spec-022 shape.

**Acceptance Scenarios**:

1. **Given** a diff that introduces `query = "SELECT ... " + request.args["id"]`, **When** the workflow runs, **Then** the record holds one `injection` finding at that line with severity high, routing implementer, evidence equal to a line of the diff, and the verdict is `security_failed`.
2. **Given** the model reports the same finding at a line outside the diff with invented evidence, **When** the gate runs, **Then** the finding is dropped and recorded, the single re-anchor call runs, and a correctly anchored restatement survives.
3. **Given** a finding whose `introduced_by` is not a changed file, **When** the gate runs, **Then** the finding is dropped.
4. **Given** the workflow reports `security_failed`, **When** coordinare dispatches the implementer, **Then** the blocking findings are on the implementer's payload in the repair-lane carrier and the implementer plan selects the repair lane.

---

### User Story 2 - A committed secret is blocking whatever the model thinks (Priority: P1)

A PR commits an API key. The scanner reports it; the model may or may not. The finding is critical, blocking, and cannot be dropped by the anchor gate because it comes from a tool with a line number.

**Why this priority**: scanner findings are the floor; moving the scan into the performer must not weaken it.

**Independent Test**: run the `secret` fixture with a fake scanner that returns one hardcoded-secret finding and a model that returns nothing; the verdict is `security_failed` and the record shows the finding with tool `semgrep`.

**Acceptance Scenarios**:

1. **Given** the scanner reports a critical finding and the model reports none, **When** the gate runs, **Then** the finding survives with `tool=semgrep`, `origin=rule`, severity critical, and the verdict is `security_failed`.
2. **Given** the scanner and the model report the same `(file, line, category)`, **When** the gate runs, **Then** one finding remains, carrying the tool's severity.
3. **Given** the model attaches a `downgrade_reason` to a `hardcoded_secret` finding, **When** the gate runs, **Then** the downgrade is applied to the model's finding only, recorded as `downgraded=true` with the reason, and any scanner finding at the same anchor keeps its severity.

---

### User Story 3 - No scanner, no pass (Priority: P1)

The performer image is missing semgrep, or bandit crashes, or a tool times out. The round ends as an environment hold naming the tool; no review is posted, nothing is lifted, and the card does not advance.

**Why this priority**: the fail-closed guarantee moved from coordinare into the workflow; losing it would make a broken image a silent pass.

**Independent Test**: run the `scanner_unavailable` fixture whose fake scanner reports the binary missing; the record's verdict is `env_blocked`, the reason names `semgrep`, the poster was never called, and main.py maps it to `env_blocked`.

**Acceptance Scenarios**:

1. **Given** the semgrep binary is not on the path, **When** the scan step runs, **Then** the round ends `env_blocked` with a reason naming semgrep and no model call is made.
2. **Given** bandit exits non-zero with unparseable output, **When** the scan step runs, **Then** the round ends `env_blocked` naming bandit.
3. **Given** a scanner exceeds its time budget, **When** the scan step runs, **Then** the round ends `env_blocked` naming the tool and the budget.

---

### User Story 4 - A clean change passes only after the code was read, advisories in one comment (Priority: P2)

A PR hashes a token with a weak algorithm but introduces nothing blocking. The stage passes, posts one COMMENT review listing the advisory, and lifts nothing.

**Why this priority**: advisories must still reach the human without bouncing the card or spamming comments.

**Independent Test**: run the `advisory_only` fixture; verdict `security_passed`, one COMMENT review whose body names the `weak_crypto` advisory, no inline comments, nothing lifted.

**Acceptance Scenarios**:

1. **Given** only medium findings survive, **When** the workflow posts, **Then** the event is COMMENT and the body lists each advisory with its path and line.
2. **Given** no blocking finding and a changed file that was neither fully in the diff nor opened by the survey, **When** the gate runs, **Then** the verdict is `env_blocked` naming the unread file, not `security_passed`.
3. **Given** a blocking category with a `downgrade_reason`, **When** the gate runs, **Then** the finding is advisory, the record shows the downgrade, and the review body names it as downgraded.

---

### Edge Cases

- The diff is truncated: the coverage pass runs once naming the unread files; approval still requires every changed file read or opened.
- A finding anchors in an unchanged file the survey opened (a sink reached from a changed source): it is kept, with `introduced_by` naming the changed file, and it lands in the review body because it is outside every hunk.
- The model returns more than 30 findings: schema violation, one reprompt, then the malformed-output retry path as for every workflow.
- The scanner reports a finding in a file that is not in the diff (a tool scanning a whole file): kept as a rule finding, because the tool is authoritative and the anchor is real.
- The PR URL is missing or the review post fails: `env_blocked` with the reason, nothing lifted, as for the reviewer.
- The security role is dispatched without the workflow flag: coordinare runs the spec-083 dispatch scan and monitor floor unchanged; the prose path commits the report and posts advisories as today.
- Two security rounds on one card: the cycle limit (`SECURITY_MAX_CYCLES`) still blocks the card after the configured number of failures.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: security` on the security role, the security stage MUST run intake, scan, survey, findings, gate, post, report, in that order, advanced by code.
- **FR-002** Intake MUST parse the injected diff into changed files with new-side hunk ranges, record whether the diff was truncated, and carry the implementation brief.
- **FR-003** The scan step MUST run semgrep and bandit over the changed files inside the performer, normalise their output to `{severity, category, description, file, line}` with the same category derivation the coordinare scanner uses today, and record per tool the exit code, finding count and duration.
- **FR-004** A missing binary, non-zero exit with unparseable output, timeout, or malformed output from either scanner MUST end the round as `env_blocked` naming the tool, before any model call, with no review posted.
- **FR-005** The survey MUST run only commands the spec-165 allow-list accepts, under a command and output budget, record every command and refusal, track which files were opened, and be seeded with the changed files and the scan findings.
- **FR-006** When the diff was truncated or a changed file was neither fully in the diff nor opened, the workflow MUST run exactly one more survey turn naming the unread files.
- **FR-007** The findings call MUST be one schema-guarded model call with one reprompt. Each finding carries path, line, a category from the configured security set, problem, why blocking, verbatim evidence, `introduced_by` naming a changed file, and an optional `downgrade_reason`; at most 30 findings. The schema MUST reject severity, routing and verdict keys.
- **FR-008** The gate MUST drop any model finding whose path is neither a changed file nor a file the survey opened, whose `introduced_by` is not a changed file, or whose evidence matches no line of the diff or the surveyed output; dropped findings MUST be recorded and re-anchored with exactly one further call whose output is gated again.
- **FR-009** The gate MUST assign severity from the category table (`hardcoded_secret` critical; `injection`, `broken_authorization`, `insecure_deserialization`, `path_traversal`, `ssrf` high; `weak_crypto`, `missing_hardening`, `information_leak`, `other_insecure_pattern` medium) and routing from the category (`broken_authorization` to architect, all others to implementer).
- **FR-010** A `downgrade_reason` on a model finding in a blocking category MUST lower it to medium, mark it `downgraded=true`, and keep the reason on the record; it MUST never apply to a scanner finding.
- **FR-011** Scanner findings MUST join the surviving findings as `origin=rule` with the tool's severity and name, MUST never be dropped by the anchor rule, and MUST collapse duplicates against model findings on `(file, line, category)`, keeping the tool's severity.
- **FR-012** The gate MUST derive the verdict: any critical or high finding is `security_failed`; none is `security_passed`; passing additionally requires full coverage, else `env_blocked` naming the unread files.
- **FR-013** The workflow MUST post exactly one GitHub review: `REQUEST_CHANGES` with an inline comment per blocking finding inside a hunk and the rest in the body when any blocking finding exists, else `COMMENT` whose body lists the advisories and any downgrade. No thread MUST be resolved; the committed `security.md` report and the per-finding advisory comments MUST NOT be produced under the workflow.
- **FR-014** The performer MUST report `security_passed` or `security_failed` with the findings in the spec-022 list shape (severity, category, description, file, line, routing) so coordinare's routing to implementer or architect is unchanged; a hold MUST be `env_blocked` with nothing lifted; the `SECURITY_MAX_CYCLES` limit MUST apply as today.
- **FR-015** Coordinare MUST lift the blocking findings routed to the implementer into the per-card `review_findings` carrier (a record in the spec-169 shape) on `security_failed` from the security stage, so the next implementing dispatch runs the repair lane; the dispatch-payload contract MUST record the new producer.
- **FR-016** When the security role runs the workflow, coordinare MUST NOT run the spec-083 dispatch-time diff fetch and scan nor the monitor floor merge for that dispatch; without the workflow both MUST run unchanged.
- **FR-017** The security workflow MUST commit nothing and its report MUST record the executed write-free check.
- **FR-018** Every step MUST log its duration and every model call its elapsed time and completion tokens, in the events specs 164 to 169 emit.
- **FR-019** Every gate rule MUST be a pure function with its own test, shown to fail under a mutation, including the severity and routing tables.
- **FR-020** The scanner normaliser in the performer package MUST produce, over the shared scanner fixtures, exactly the findings coordinare's prose-path scanner produces, enforced by a parity test; the coordinare daemon image does not ship the performer package, so the two copies are held equal by test rather than by import.

### Key Entities

- **SecurityFinding**: a reviewer Finding (path, line, category, problem, why_blocking, evidence, origin) plus severity, routing, introduced_by, tool (`model`, `semgrep`, `bandit`), downgraded and downgrade_reason.
- **ScanResult**: per tool, the exit code, finding count, duration and error, if any.
- **SecurityRecord**: changed files, truncation, scan results, survey commands and refusals, findings before the gate, dropped, re-anchored, blocking and advisory survivors, coverage pass outcome, unread files, verdict, post result.
- **Category table**: the fixed map from category to default severity and routing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Every finding in every posted security review anchors to a line the record shows the workflow saw, on all fixtures and the first ten live rounds.
- **SC-002** Zero `security_passed` verdicts with a critical or high scanner finding, and zero with a scanner unavailable, on all fixtures and live rounds.
- **SC-003** Zero passes without full coverage.
- **SC-004** A security round completes in under ten minutes at the 90th percentile including container start, over the first ten live rounds.
- **SC-005** Every implementer dispatch that follows a `security_failed` carries the blocking findings and runs the repair lane.
- **SC-006** The six fixtures (`clean`, `injection`, `secret`, `scanner_unavailable`, `advisory_only`, `downgrade`) pass deterministically in CI, and `clean` and `injection` pass live through the gateway.

### Performance budgets (Constitution IV, provisional until measured live)

- Scan: 120 seconds per tool.
- Survey: 12 commands, 4000 characters kept per command, one coverage pass.
- Findings: one call at 8000 completion tokens plus one reprompt; one re-anchor call.
- Whole round under ten minutes at p90 including container start.

## Assumptions

- semgrep and bandit are present in the full performer image (spec 083 FR-007); the scan step only verifies and uses them.
- The reviewer's diff parser, survey, schema builder, anchor rules and review poster are reusable as imports without change to the reviewer.
- The spec-169 `review_findings` carrier accepts a record produced by the security stage; the repair lane needs only `findings` with paths.
- Coordinare already routes `security_failed` by the `routing` field and handles `env_blocked` from any verdict stage.

## Rollout

Default off. Enable per symphony with `workflow: security` on the security role. The first live rounds run on a low-traffic symphony with the prose path still available by removing the line.
