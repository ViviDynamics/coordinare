# Feature Specification: Security Performer

**Feature Branch**: `022-security-performer`
**Created**: 2026-03-18
**Status**: Draft

## Overview

The security performer is the sixth role in the sequential performer lifecycle (after reviewer approval). It performs an automated security review of the feature branch, analysing code changes for vulnerabilities, misconfigurations, secret leakage, and insecure patterns. Findings are categorised by severity: critical and high findings block lifecycle advancement; medium and low findings are posted as advisory comments without blocking. Findings are routed intelligently: code-level vulnerabilities route back to the implementer; architecture-level vulnerabilities route back to the architect. The lifecycle re-runs affected downstream roles after each fix cycle.

The security performer does not replace a formal penetration test or a human security audit — it is a fast, automated first pass that catches common vulnerability classes before the feature reaches a human reviewer.

## Clarifications

### Session 2026-03-18

- Q: What severity levels are used? → A: Four: critical, high, medium, low. Critical and high are always blocking; medium and low are advisory.
- Q: How are findings routed? → A: Code-level findings (injection, secret leakage, insecure API usage) route to the implementer. Architecture-level findings (insecure design, missing threat mitigations) route to the architect. The coordinare's `classify_human_feedback` routing logic (019) handles this based on the security performer's `routing` field in each finding.
- Q: What vulnerability categories does the security performer check? → A: At minimum: OWASP Top 10, secret/credential leakage, dependency vulnerabilities (known CVEs), insecure defaults, missing authentication/authorisation checks, insecure data handling.
- Q: Does the security performer modify code? → A: No. It only reports findings. The implementer (or architect) is responsible for making the actual fix.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Pass a Clean Feature Branch (Priority: P1)

When the feature branch has no critical or high severity findings, the security performer returns `security_passed` and the lifecycle advances to QA.

**Why this priority**: The clean-pass case is the happy path that must work reliably for the lifecycle to function at all.

**Independent Test**: Can be tested by dispatching the security performer with a branch that has no known vulnerabilities, verifying it returns `security_passed` and posts only advisory comments (or none) to the PR.

**Acceptance Scenarios**:

1. **Given** the security performer receives a dispatch for a branch with no critical or high findings, **When** the AI backend completes its analysis, **Then** the performer returns `{"status": "security_passed"}` and the lifecycle advances to QA.
2. **Given** there are medium or low findings alongside the overall pass, **When** the performer returns, **Then** advisory comments are posted to the GitHub PR but the lifecycle is NOT blocked.
3. **Given** the security performer returns `security_passed`, **When** the coordinare processes the result, **Then** `performer_stage` advances to the next configured role (qa) and the card remains "In Progress".

---

### User Story 2 — Block on Critical or High Severity Findings (Priority: P1)

When the feature branch has one or more critical or high severity findings, the security performer blocks the lifecycle and routes each finding to the appropriate role (implementer or architect) for remediation.

**Why this priority**: Security blocking is the core value proposition of this performer role. Without it, vulnerabilities silently reach production.

**Independent Test**: Can be tested by dispatching the security performer with a branch that contains a known SQL injection pattern, verifying it returns `security_failed` with at least one critical or high finding routed to the implementer.

**Acceptance Scenarios**:

1. **Given** the branch contains a critical severity finding (e.g., hardcoded API secret), **When** the security backend identifies it, **Then** the performer returns `{"status": "security_failed", "findings": [{"severity": "critical", "category": "secret_leakage", "description": "...", "file": "...", "line": ..., "routing": "implementer"}]}`.
2. **Given** the security performer returns `security_failed`, **When** the coordinare processes the result, **Then** code-level findings are relayed to the implementer performer, architecture-level findings are relayed to the architect performer, and the appropriate performer is re-dispatched.
3. **Given** the implementer fixes a security finding and the security performer is re-dispatched, **When** no blocking findings remain, **Then** the performer returns `security_passed` and the lifecycle advances.
4. **Given** blocking findings persist after a configurable maximum fix cycle limit, **When** the limit is reached, **Then** the security performer returns `{"status": "blocked", "questions": ["..."]}` for human attention.

---

### User Story 3 — Advisory Comments for Medium/Low Findings (Priority: P2)

The security performer posts advisory comments for medium and low severity findings without blocking the lifecycle. These comments are visible to the human reviewer for awareness but do not require automated remediation.

**Why this priority**: Advisory findings are informational — they improve code quality without creating friction. Surfacing them passively respects the human reviewer's judgment about whether to act on them.

**Independent Test**: Can be tested by dispatching the security performer with a branch that has only medium-severity findings, verifying the performer returns `security_passed` while still posting advisory comments to the PR.

**Acceptance Scenarios**:

1. **Given** only medium or low severity findings are detected, **When** the performer completes, **Then** it returns `security_passed` and posts each finding as a PR comment labelled `[Advisory]`.
2. **Given** both critical and advisory findings are detected, **When** the performer returns `security_failed`, **Then** advisory findings are included in the findings list with appropriate severity labels but are NOT included in the routing payload sent to the implementer.

---

### Edge Cases

- What if the security analysis tool has no opinion (no findings, no pass) — how is an inconclusive result handled?
- What if the same vulnerability persists across multiple fix cycles because the root cause is in a shared library (not directly in the diff)?
- What if a finding is a false positive — can the implementer annotate the code to suppress it?
- What if the security performer cannot access the repository (network error, bad token)?
- What if the architecture plan is absent (architect was skipped) — does the security performer still run?
- What if a dependency vulnerability (CVE) has no available fix at the current version — what is the expected outcome?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The security performer MUST implement the full coordinare wire protocol: dispatch, status, relay_feedback, and health actions.
- **FR-002**: On a `dispatch` action, the security performer MUST clone the repository, check out the feature branch, and begin AI-driven security analysis of the diff.
- **FR-003**: The security performer MUST check for at minimum: OWASP Top 10 vulnerabilities, hardcoded secrets or credentials, dependency CVEs (where the backend supports it), insecure default configurations, and missing authentication or authorisation checks.
- **FR-004**: Each finding MUST include: `severity` (critical/high/medium/low), `category`, `description`, `file`, `line` (where applicable), and `routing` (implementer/architect).
- **FR-005**: If any finding has severity `critical` or `high`, the performer MUST return `{"status": "security_failed", "findings": [...]}`. The lifecycle MUST NOT advance past this state until all blocking findings are resolved.
- **FR-006**: If no findings have severity `critical` or `high`, the performer MUST return `{"status": "security_passed"}` even if medium or low findings exist.
- **FR-007**: Medium and low findings MUST be posted as advisory PR comments (labelled `[Advisory - Security]`) regardless of the overall pass/fail outcome.
- **FR-008**: When routing findings: findings with `routing: "implementer"` MUST be delivered to the implementer via `relay_feedback`; findings with `routing: "architect"` MUST be delivered to the architect via `relay_feedback`. The coordinare processes this routing based on the `routing` field.
- **FR-009**: The security performer MUST enforce a configurable maximum fix cycle limit (env var `SECURITY_MAX_CYCLES`, default: 3). When the limit is reached, it MUST return `{"status": "blocked", "questions": ["..."]}`.
- **FR-010**: The security performer MUST be independently configurable in `config.yaml` under `performers.security`.
- **FR-011**: If the architecture plan file is present on the branch, the security performer MUST include it in its analysis context for detecting architecture-level security issues.

### Key Entities

- **Finding**: A single identified security issue — includes severity, category, description, file/line reference, and routing target.
- **security_passed**: Terminal success state — no blocking findings; lifecycle advances to QA.
- **security_failed**: Non-terminal outcome — one or more blocking findings; coordinare routes to implementer or architect for remediation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of security sessions with critical or high findings return `security_failed` and route at least one finding to the appropriate performer.
- **SC-002**: 100% of security sessions with only medium/low findings return `security_passed` while posting advisory comments to the PR.
- **SC-003**: Advisory comments are posted to the GitHub PR within one coordinare polling cycle of the `security_passed` response.
- **SC-004**: When findings are routed to the implementer, the implementer receives them as structured relay_feedback within one coordinare polling cycle.
- **SC-005**: After a fix cycle resolves all critical and high findings, the security performer returns `security_passed` on re-dispatch in at least 90% of test cases with intentionally fixed vulnerabilities.
- **SC-006**: The maximum fix cycle limit prevents infinite security-remediation loops in 100% of cases.

## Assumptions

- The security performer does not execute the application code — it is a static analysis role only. Dynamic security testing (DAST) is out of scope.
- The coordinare's lifecycle (019-performer-lifecycle) is responsible for routing findings to the correct performer; the security performer only declares the `routing` field, it does not invoke the implementer or architect directly.
- Dependency CVE scanning depends on the AI backend's knowledge and available tooling. If the backend cannot scan dependencies, this capability is advisory-only and the absence of CVE findings does not imply a clean dependency tree.
- False positive suppression (e.g., inline code annotations) is a future enhancement and is out of scope for this spec.
