# Implementation Plan: QA Evidence Integrity, Toolchain Availability & Visual-Evidence Capture

**Branch**: `120-qa-evidence-integrity` | **Date**: 2026-06-25 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/120-qa-evidence-integrity/spec.md`

## Summary

Harden the QA stage end-to-end so an unsubstantiated "pass" can never advance a card, the QA
performer reliably gets the project's cached toolchain on PATH, and visual changes are proven with
at least one screenshot. Three coordinated changes, grounded in root-cause analysis ([research.md](./research.md)):

1. **US1 — Verdict integrity (P1):** fix the performer's env-limited *advisory-pass* path so a run
   that passed **zero** criteria under env limits is `qa_env_blocked`, not `qa_passed`; and add a
   coordinare-side gate that refuses to advance a `qa_passed` whose report shows 0/N criteria passed
   (or missing required visual evidence), routing to HOLD (env signal present) or bounce (otherwise).
2. **US2 — Toolchain on PATH (P1):** make `_activate_env_cache` resolve `$DEVENV` deterministically
   (clear the `_DEVENV_SOURCED` re-entry guard, export `DEVENV`) so `activate.sh` actually captures
   Ruby/rbenv into `cache_env`; assert the toolchain resolves post-activation and surface an
   environment error if not; emit a per-dispatch observability record.
3. **US3 — Visual evidence (P2):** make the QA persona free to use any in-image browser tooling for
   in-turn capture (primary), harden the post-QA docker screenshot node to run only with boot-proof
   and never report empty success (backstop); enforcement (no screenshot ⇒ no pass) rides on US1.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — pydantic 2.x (PerformerResponse/report contract, no schema
migration), langgraph (`monitor_performer`/`qa_screenshots` nodes), structlog (observability),
the performer `workspace.py` activation layer, the `claude_code` backend + `_env_policy`,
`persona_service` (qa persona). **No new external dependency** (Chromium/Playwright already in `:extra`;
docker screenshot node already exists).
**Storage**: existing single-host JSON snapshot via `state_store.py`. **No schema migration** — the
coordinare gate reads existing QA-report fields; any new flag is a backward-compatible default
(parallel to 088 bootstrap fields / 095 ENV_BLOCKED field).
**Testing**: `.venv/bin/pytest` (unit + integration under `tests/`); `.venv/bin/ruff check` for lint.
**Target Platform**: Linux performer containers (`coordinare-performer:extra`) + the coordinare daemon.
**Project Type**: single (coordinare daemon + performer package in one repo).
**Performance Goals**: no measurable regression to QA dispatch/monitor latency; the coordinare gate is
an in-memory dict read (O(1)); the toolchain assertion adds one `command -v`-class check to activation.
**Constraints**: secret invariant (names/ids/counts/reasons only); only humans approve PRs; reuse
existing `qa_env_blocked`/`qa_failed` states + HOLD/notify machinery (no new lifecycle states).
**Scale/Scope**: affects the QA stage of all symphonies; validated against the website symphony.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — ✅ Changes are localized, single-responsibility: one performer
  classification fix, one coordinare gate function, one activation fix, one persona edit, one node
  hardening. No dead code; type annotations on new helpers; reuses existing constants/states.
- **II. Testing Discipline (NON-NEGOTIABLE)** — ✅ TDD: each FR gets failing unit tests first
  (synthetic QA reports for the gate; activation-env tests for US2; node-reachability tests for US3).
  Deterministic (no live containers — fixtures/mocks). Coverage must not regress.
- **III. UX Consistency** — ✅ Operator-facing surfaces (PR comment verdict + notifications) become
  *more* consistent: an unverified run reads as ENVIRONMENT-BLOCKED/FAILED, never PASSED. Error
  reasons are actionable and carry no internal secrets.
- **IV. Performance by Design** — ✅ Budget: coordinare gate adds an O(1) dict read; activation adds
  one bounded toolchain check; no new network calls. Success Criteria (SC-001..006) are the measurable
  outcomes; no tracked perf path is degraded.
- **V. Clarity Before Action** — ✅ Root causes pinned in research.md from verbatim code; scope
  bounded (cache contents/image/managed-services explicitly out). No `NEEDS CLARIFICATION` remain.

**Result: PASS** (no violations; Complexity Tracking not required).

## Project Structure

### Documentation (this feature)

```text
specs/120-qa-evidence-integrity/
├── plan.md              # This file
├── research.md          # Root-cause analysis + decisions (done)
├── data-model.md        # QA verdict / evidence / env-health entities (done)
├── quickstart.md        # How to validate the three stories (done)
├── contracts/
│   └── qa-report.md     # The QA report field contract the coordinare gate reads (done)
├── checklists/
│   └── requirements.md  # Spec quality checklist (done, passing)
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
# US1 — verdict integrity
agent/performer/src/performer/main.py        # env-limited advisory-pass fix (criteria_passed==0 → qa_env_blocked)
src/coordinare/graph/nodes/monitor_performer.py # coordinare-side gate before advancing qa_passed; reuse qa_env_blocked HOLD path

# US2 — toolchain on PATH
agent/performer/src/performer/workspace.py   # _activate_env_cache: export DEVENV, clear _DEVENV_SOURCED, assert toolchain, observability
agent/performer/src/performer/main.py        # surface activation/toolchain failure as environment_error in the QA report

# US3 — visual evidence
src/coordinare/services/persona_service.py    # qa persona: "use any in-image browser tooling" wording
src/coordinare/graph/nodes/qa_screenshots.py  # run only with boot-proof; never report empty success

# Tests (TDD — written first)
tests/unit/...                                # gate logic, activation env, node reachability, persona text
tests/integration/...                         # end-to-end qa-monitor routing for the three downgrade cases
```

**Structure Decision**: Single repo, existing module boundaries. The performer changes
(`main.py`, `workspace.py`, persona consumed by the performer) and the coordinare changes
(`monitor_performer.py`, `qa_screenshots.py`, `persona_service.py`) are edited in place — no new
modules. The performer-side and coordinare-side US1 fixes are deliberately *both* present (defense in
depth per research Decision 1); they are independently testable.

## Phase ordering & independence

- **US1** and **US2** are both P1 and independently testable. US1 (the gate) can land and be tested
  with synthetic reports without US2. US2 (activation) can land and be tested via activation-env unit
  tests without US1. They compose: US2 makes the run *substantive*; US1 ensures a *non*-substantive
  run can't pass.
- **US3** (P2) depends on US2 at runtime (the app must boot to capture), but its code (persona text +
  node hardening) is independently testable and enforced by US1.
- Recommended implementation order: **US1 → US2 → US3** (ship the safety gate first even though it's
  one combined spec; it's the highest-risk hole), then re-verify end-to-end on the website symphony.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Coordinare gate regresses *legitimate* passes (FR-006/SC-005) | Gate only fires on `criteria_checked>0 AND criteria_passed==0` (or missing required visual evidence); ≥1 pass with evidence advances unchanged. Explicit regression tests. |
| `criteria_checked==0` legitimate (nothing to verify) misclassified | Gate keys on `checked>0 AND passed==0`; a genuine no-criteria scope (`checked==0`) is untouched (edge case in spec). |
| Activation `DEVENV` fix breaks other stages (implementer/reviewer) that also call `_activate_env_cache` | The fix only *adds* a correctly-resolved `$DEVENV` + clears a guard that was already cleared for services; it cannot remove paths. Run the full activation test suite across stages. |
| Toolchain assertion false-positives on caches without rbenv (non-Ruby projects) | Assertion is driven by what the cache *advertises* (activate.sh references), not a hardcoded "ruby"; a cache with no rbenv reference asserts nothing. |
| Stale deployed performer image still emits the old advisory pass | The coordinare gate (defense in depth) catches it regardless of performer build. |

## Post-Design Constitution Re-check

After Phase 1 design (data-model/contracts/quickstart): still **PASS**. No new dependencies, no new
persisted schema, no new lifecycle states, changes are localized and test-first. Complexity Tracking
table intentionally empty (no violations to justify).
