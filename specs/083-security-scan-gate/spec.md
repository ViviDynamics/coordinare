# Feature Specification: Security Scan Gate

**Feature Branch**: `083-security-scan-gate`
**Created**: 2026-06-08
**Status**: Draft
**Input**: Approved design: `docs/superpowers/specs/2026-06-07-security-scan-gate-design.md`

## Overview

The `security` performer role currently produces a **pure-LLM verdict**: the model
receives the PR diff, is told to look for OWASP Top 10 / insecure patterns, and emits
`{"passed": bool, "findings": [...]}`. There is no deterministic backstop. The 082
executor sweep proved the failure mode — all five tool-capable local executors
(qwen3.6:35b, qwq:32b, qwen2.5:14b-instruct, qwen2.5:32b, qwen3-coder:30b) wrongly
**passed** a deliberately vulnerable bench PR; only gpt-oss:120b caught it. Production
binds `security` to gpt-oss:120b today, so prod is presently safe — but the safety rests
on a single model with no floor underneath it.

This feature makes the `security` verdict **no longer purely model judgment** by adding a
deterministic static-analysis floor plus structural and config-level reinforcements.
**One sentence:** *a critical/high static-analysis hit must block the PR regardless of
what the model concludes.*

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deterministic static-analysis floor (Priority: P1)

As the coordinare enforcing the `security` role, I want a deterministic static-analysis
scan (semgrep + bandit) run over the PR diff whose critical/high findings **force**
`security_failed` regardless of the model's verdict, so that no single model's opinion is
the only thing standing between a vulnerable PR and merge.

**Why this priority**: This is the load-bearing fix — detection power independent of the
model. It directly resolves the 082 failure mode and carries the end-to-end acceptance
test. P2/P3 are complements that are worthless without this floor.

**Independent Test**: Feed a fixture diff containing a known injection through the gated
security role with a model that returns `passed: true`; verify the coordinare overrides to
`security_failed` and merges the scanner findings into `relay_feedback`. Fully testable
without P2/P3.

**Acceptance Scenarios**:

1. **Given** a PR diff with a critical/high static-analysis finding, **When** the security
   model returns `passed: true`, **Then** the coordinare forces `security_failed` and the
   scanner findings appear in `relay_feedback`.
2. **Given** a PR diff with only medium/low static-analysis findings, **When** the security
   model returns `passed: true`, **Then** the verdict is **not** overridden.
3. **Given** a clean PR diff, **When** the scanner runs, **Then** it yields no findings and
   the model's verdict stands.
4. **Given** a security dispatch, **When** the coordinare scans the diff once at dispatch,
   **Then** the same stashed findings are reused for both prompt-injection (`card_context`)
   and verdict-floor enforcement — no double scan.

---

### User Story 2 - CWE taint→sink checklist (Priority: P2)

As an operator relying on whatever model is bound to `security`, I want the security
persona prompt restructured into a CWE taint→sink checklist, so that every judge —
regardless of capability — reasons through sources, sinks, and a fixed CWE list before
emitting its verdict.

**Why this priority**: Model-agnostic ceiling-raiser. Small, independent prompt change.
Valuable but not load-bearing — the floor (P1) is what actually guarantees safety.

**Independent Test**: Assert the rendered `security` persona string contains the
taint→sink checklist structure (sources, sinks, fixed CWE list) while still requiring the
unchanged `{passed, findings[]}` JSON output contract.

**Acceptance Scenarios**:

1. **Given** the security persona is rendered, **When** inspected, **Then** it instructs the
   model to enumerate untrusted sources, trace them to dangerous sinks, walk a fixed CWE
   list, and emit the same `{passed, findings[]}` JSON.

---

### User Story 3 - Higher-capability judge gate (Priority: P3)

As an operator, I want config load-time validation that rejects binding the `security` role
to a known-weak model, so that a future config edit cannot silently remove the only capable
judge.

**Why this priority**: Codifies the production lever. Small, independent, load-time-only.
Complements the floor but does not replace it.

**Independent Test**: Load a config that binds `security` to a denylisted model → expect a
validation error. Load a config with a capable model → no error.

**Acceptance Scenarios**:

1. **Given** a config that resolves the `security` role to a denylisted model (one of the
   five 082 executors), **When** the config is loaded, **Then** a validation error is raised.
2. **Given** a config that resolves `security` to a non-denylisted model, **When** loaded,
   **Then** no error is raised.
3. **Given** a config that binds reviewer/assessor to a denylisted model, **When** loaded,
   **Then** no error is raised (P3 scope is `security` role only).

---

### Edge Cases — FAIL-CLOSED

- **Scanner unavailable / crashes / times out, or diff fetch fails**: the gate is
  **fail-closed**. Coordinare emits `security_failed` carrying a synthetic **critical**
  finding `category: scanner_unavailable` with a clear description and a loud observability
  marker. To avoid a pointless implementer fix-loop (an implementer cannot fix broken
  tooling), this finding **routes to halt/human attention** rather than back to
  `implementer`. The floor is never silently absent.
- **Non-Python/JS diffs**: semgrep `--config auto` covers many languages; bandit is
  Python-only and simply yields nothing for other languages — not an error.
- **Secrets discipline**: the scanner sees only repo diff content (review material, not
  secrets) and must never echo `auth_env`-resolved values. Diff text and any tokens stay
  out of INFO logs (FR-019 discipline from 073/080). INFO limited to scan summary
  (counts/severities), not raw diff.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST run a deterministic static-analysis scan (semgrep + bandit)
  over the PR's changed files when `role == "security"`, executed **once** at dispatch.
- **FR-002**: The scanner MUST normalize semgrep and bandit output into the existing
  spec-022 finding schema (`severity`, `category`, `description`, `file`, `line`, `routing`),
  defaulting scanner findings to `routing: implementer`.
- **FR-003**: The coordinare MUST stash the scan results in transient graph state
  (`state["scanner_findings"]`) — no new persisted state.
- **FR-004**: The coordinare MUST inject the stashed findings into `card_context` as a
  `scanner_findings` block so the security model reasons over real scanner output.
- **FR-005**: Before accepting `security_passed`, the coordinare MUST re-read the same stashed
  findings and, if any are critical/high, **force `security_failed`** and merge the scanner
  findings into `relay_feedback` — regardless of the model's `passed` value.
- **FR-006**: The coordinare MUST reuse the single dispatch-time scan for both
  prompt-injection and verdict-floor enforcement (run-once, reuse). No double coordinare scan.
- **FR-007**: semgrep + bandit MUST be installed in `agent/performer/Dockerfile.full` (the
  layer that already carries ruff/black/shellcheck/eslint; the `Dockerfile.base` source-COPY
  invariant MUST be unaffected) and exposed as an **advisory** performer tool that feeds the
  model's own `findings[]` but does NOT bind the verdict.
- **FR-008**: The gate MUST be **fail-closed**: if the scanner is unavailable, crashes, times
  out, or the diff fetch fails, the coordinare MUST emit `security_failed` with a synthetic
  critical `scanner_unavailable` finding routed to halt/human attention plus a loud
  observability marker.
- **FR-009**: The `security` persona prompt MUST be restructured into a CWE taint→sink
  checklist (enumerate untrusted sources → trace to dangerous sinks → walk a fixed CWE list:
  injection, broken authz, hardcoded secrets, insecure deserialization, path traversal, SSRF)
  while preserving the unchanged `{passed, findings[]}` JSON output contract.
- **FR-010**: Config load MUST raise a validation error if the `security` role resolves to a
  model on a known-weak denylist (qwen3.6:35b, qwq:32b, qwen2.5:14b-instruct, qwen2.5:32b,
  qwen3-coder:30b). Scope is the `security` role only; reviewer/assessor are out of scope.
- **FR-011**: The scanner MUST NOT echo `auth_env`-resolved values or raw diff text into INFO
  logs; INFO is limited to scan summary (counts/severities).
- **FR-012**: No protocol schema change — reuse `ProtocolResponse.findings` and the
  `security_passed`/`security_failed` status enum.

### Key Entities

- **Finding**: a normalized static-analysis result — `severity`, `category`, `description`,
  `file`, `line`, `routing`. Shared with the spec-022 finding schema used by model findings.
- **scanner_findings (graph state)**: transient list of `Finding` stashed at dispatch and
  consumed at verdict time. Mirrors `relay_feedback` lifecycle — not persisted.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Re-running the 082 vulnerable bench PR through the gated security role yields
  `security_failed` — the acceptance bar for the whole feature.
- **SC-002**: A critical/high scanner finding overrides a model `passed: true` verdict to
  `security_failed` in 100% of cases (unit-test enforced).
- **SC-003**: Scanner-unavailable / diff-fetch-failure conditions produce `security_failed`
  (fail-closed) in 100% of cases — never a silently-absent floor.
- **SC-004**: A config binding `security` to any of the five denylisted models fails to load.
- **SC-005**: No raw diff content or resolved secret values appear in INFO-level logs.

## Decomposition & Order

P1 → P2 → P3 within one spec. P1 is load-bearing and MUST land first (it carries the e2e
acceptance test SC-001). P2 and P3 are small, independent, and can land in any order after P1.

## Non-Goals

- No change to reviewer/assessor judge binding (P3 is `security` role only).
- No new persisted coordinare state (scanner findings live in transient graph state).
- No replacement of the model verdict — the model still reviews; the scanner adds a floor
  and feeds context.
