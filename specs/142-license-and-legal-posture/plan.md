# Implementation Plan: License and Legal Posture for Public Release

**Branch**: `142-license-and-legal-posture` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/142-license-and-legal-posture/spec.md`
**Issue**: [#196](https://github.com/ViviDynamics/coordinare/issues/196) (launch-blocking)

## Summary

Publish coordinare's legal posture: the Elastic License 2.0 at the repository root, a single
copyright statement, a contribution policy that welcomes issues and refuses public pull
requests, a private security reporting route, and the two pieces of evidence needed before the
repository can be made public (a dependency license audit and an executed pre-public scrub).

The technical work is small and falls into three buckets. **Documents** are new files at the
repository root plus a rewritten README section. **Automation** is one new GitHub Actions
workflow that responds to external pull requests, plus GitHub-native templates. **Enforcement**
is two pure-Python checks added to the existing unit suite, so the posture cannot silently rot:
one that fails if any public-facing document claims coordinare is open source, and one that fails
if a distributed dependency's license falls outside the accepted set.

Nothing here touches `src/coordinare/`. No new runtime dependency is added: the dependency audit
walks `uv.lock` with stdlib `tomllib` and reads license metadata via stdlib
`importlib.metadata`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv). Workflow is YAML
for GitHub Actions. Documents are Markdown and plain text.
**Primary Dependencies**: None added. Enforcement uses stdlib only (`tomllib`,
`importlib.metadata`, `pathlib`, `re`) plus the existing `pytest`.
**Storage**: N/A. No persisted coordinare state, no schema change, no `state_store.py` touch.
**Testing**: `pytest` via the existing suite. New module
`tests/unit/test_142_license_and_legal_posture.py`, following the naming and structure of
`tests/unit/test_132_onboarding_config_hardening.py`.
**Target Platform**: Repository content and GitHub Actions. The workflow needs a GitHub-hosted
runner (see research.md R4).
**Project Type**: Single project. Repository-root documents plus `.github/` plus `tests/unit/`.
**Performance Goals**: N/A. The two checks must stay fast enough to be unremarkable in the
existing suite; the dependency walk is 108 lock entries and completes in milliseconds.
**Constraints**:
- The wording guard must produce **zero** false positives against existing accurate third-party
  references (`docs/opencode-sdk.md`, `src/coordinare/services/scoring.py:102`).
- The external-pull-request workflow must never check out or execute the fork head (FR-013).
- No existing workflow may change its triggers, required status, or runtime (SC-009).
**Scale/Scope**: 6 new root-level documents, 4 GitHub templates, 1 new workflow, 3 `pyproject.toml`
license declarations, 1 README section rewrite, 1 new test module, 2 recorded evidence artifacts.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle / Gate | Assessment |
|---|---|
| **I. Code Quality First** | PASS. The only executable code is one test module and the checks it contains. Both are pure functions over file contents with no I/O beyond reads. `make lint` and `make fmt` apply as normal. |
| **II. Testing Discipline (NON-NEGOTIABLE)** | PASS, and it is the reason this feature has code at all. Every assertion in the spec that can be mechanically verified is covered: file presence (FR-001, FR-002, FR-008, FR-014), license declarations (FR-003), required and forbidden wording (FR-005, FR-005a, FR-006, FR-007, FR-024), template presence (FR-009, FR-010), the workflow's trigger and permissions shape (FR-012, FR-013), and the dependency allowlist (FR-020). Tests are written before the documents they check. |
| **III. User Experience Consistency** | PASS. Applies to the reader rather than a UI. Terminology is fixed at "source-available" and enforced (FR-006, FR-007). GitHub's own affordances (issue chooser, contributing link, pull request template) are used rather than invented conventions. |
| **IV. Performance by Design** | N/A, recorded rather than skipped. No runtime path is touched. The added tests are file reads over 108 lock entries. |
| **V. Clarity Before Action** | PASS. Eight clarifications resolved and logged in the spec before planning; zero `NEEDS CLARIFICATION` markers remain. Two items surfaced during planning that change the work are recorded in research.md (R5, R6) rather than assumed. |
| **Quality Gate 1: Lint & Format** | Applies. One Python module, one YAML file, Markdown. |
| **Quality Gate 2: Type Check** | Applies to the new test module. Fully annotated. |
| **Quality Gate 3: Unit Tests** | Applies. No skipped tests introduced. |
| **Quality Gate 4: Integration Tests** | N/A. No module interactions to integrate. |
| **Quality Gate 5: Coverage** | Applies. New code is test code plus assertions over repository content; coverage does not regress. |
| **Quality Gate 6: Performance** | N/A. No performance-critical path. |
| **Quality Gate 7: Accessibility** | N/A. No UI change. |
| **Quality Gate 8: Code Review** | Applies. One approving review from a non-author, per the merge configuration. |

**Result: PASS. No violations, so Complexity Tracking is empty and omitted.**

## Project Structure

### Documentation (this feature)

```text
specs/142-license-and-legal-posture/
├── spec.md                          # Complete: 28 FRs, 8 clarifications, 0 open markers
├── plan.md                          # This file
├── research.md                      # Phase 0: 7 decisions
├── data-model.md                    # Phase 1: in-test data shapes (no persistence)
├── quickstart.md                    # Phase 1: how to verify the posture
├── contracts/
│   └── accepted-licenses.md         # Phase 1: the allowlist contract FR-020 enforces
├── dependency-license-audit.md      # FR-018 evidence, generated then committed
├── pre-public-scrub.md              # FR-021 evidence, executed then committed
└── checklists/
    └── requirements.md              # Complete: all items pass
```

### Source Code (repository root)

```text
LICENSE                              # NEW. Verbatim ELv2, designations filled (FR-001)
NOTICE                               # NEW. Single copyright statement (FR-002)
CONTRIBUTING.md                      # NEW. Policy, not a how-to (FR-008)
SECURITY.md                          # NEW. Private route + seam for spec 144 (FR-014, FR-017)
README.md                            # EDIT. Replace the ## License stub at :241-243 (FR-005)

pyproject.toml                       # EDIT. Add license declaration (FR-003)
agent/performer/pyproject.toml       # EDIT. Add license declaration (FR-003)
packages/service_inference/pyproject.toml  # EDIT. Add license declaration (FR-003, FR-004)

.github/
├── pull_request_template.md         # NEW. States the closed-PR policy (FR-009)
├── ISSUE_TEMPLATE/
│   ├── config.yml                   # NEW. Chooser configuration (FR-010)
│   ├── bug_report.yml               # NEW (FR-010)
│   ├── feature_request.yml          # NEW (FR-010)
│   └── feedback.yml                 # NEW (FR-010)
└── workflows/
    └── external-contributions.yml   # NEW. Comment, label, close (FR-011/012/013/025)

tests/unit/
└── test_142_license_and_legal_posture.py   # NEW. Enforcement (FR-007, FR-020, presence checks)
```

**Structure Decision**: Single project, matching the repository as it stands. Every path above
was verified to exist (or verified absent, for the new files) against `main` at 2026-08-27. The
feature deliberately adds nothing under `src/coordinare/`: the enforcement lives in the test
suite because its subject is repository content, not runtime behavior, and putting it in `src/`
would imply a runtime consumer that does not exist.

The one structural judgment worth stating: the dependency-license logic lives inside the test
module rather than in `src/coordinare/`. It is invoked only by the test, has no runtime caller,
and shipping it under `src/` would enlarge coordinare's public surface for no benefit. If a
future feature needs it at runtime (a `coordinare doctor` license check, say, which spec 145
might want), promoting it is a small, deliberate move at that point.

## Phase 0: Research

See [research.md](./research.md). Seven decisions, of which two changed the plan:

- **R5** found that GitHub private vulnerability reporting is **not available on private
  repositories**, and this repository is private. FR-016 therefore cannot be satisfied now; it
  moves to the pre-public scrub as a flip-time step, and it needs the GitHub UI because neither
  available token can write it.
- **R6** found that `semgrep` is not a declared Python dependency. It is installed into the
  performer image by `agent/performer/Dockerfile.full` and invoked as a subprocess, which puts
  it in scope for the audit as redistributed image content and out of scope as a combined work.

## Phase 1: Design

See [data-model.md](./data-model.md), [contracts/accepted-licenses.md](./contracts/accepted-licenses.md),
and [quickstart.md](./quickstart.md).

## Phase 2

`/speckit.tasks` generates `tasks.md`. Not created by this command.
