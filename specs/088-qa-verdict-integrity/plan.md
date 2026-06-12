# Implementation Plan: QA Verdict Integrity & Performer Environment Reliability

**Branch**: `088-qa-verdict-integrity` | **Date**: 2026-06-11 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/088-qa-verdict-integrity/spec.md`

## Summary

Close the QA false-pass hole (a run that executed nothing posted "PASSED 4/4" on website PR #159) and the environment-reliability bug family around it. Approach: (Cluster A) introduce a third, explicit QA outcome — `qa_env_blocked` — emitted by the performer when a pass claim has no execution evidence behind it; cross-validate criteria counts against evidence; make coordinare's terminal-verdict branch consult `env_cache_health_failed`; require app-boot evidence for visual criteria; validate screenshot uploads before rendering links. (Cluster B) extract one shared backend env-merge policy (image-PATH-first, cache appended — the #110 semantics) used by all six backends; bound bootstrap retries with escalating cooldown and a terminal exhausted state; persist bootstrap success keyed by spec SHA so restarts re-verify instead of re-bootstrapping; surface secret-refresh and services-start failures as structured events.

All work is TDD (constitution Testing Discipline + project Iron Law): failing test first for every behavior change.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (result/status models, EnvCacheState), structlog (structured events), httpx (performer HTTP client), aiohttp (performer server) — no new external dependencies
**Storage**: JSON snapshot via `state_store.py` (existing) — extended with persisted bootstrap success + attempt budget fields on the env-cache state; no new store
**Testing**: `.venv/bin/pytest` (unit: `tests/unit/`, performer: `agent/performer/tests/`), `.venv/bin/ruff check`; coverage gate ≥90% (CI `--cov-fail-under=90`)
**Target Platform**: Linux/macOS single-host coordinare daemon + Docker performer containers (`coordinare-performer:full`)
**Project Type**: Single repo, two Python packages: `src/coordinare/` (orchestrator) and `agent/performer/src/performer/` (in-container agent harness)
**Performance Goals**: Verdict classification is in-process dict/list inspection (no I/O) — no new hot path. Restart-resume budget: consumer dispatch resumes < 2 min with an intact verified cache (SC-004) vs ~13 min today.
**Constraints**: Backward compatible with in-flight cards and existing state snapshots (missing new fields must default safely); no change to honest-pass/honest-fail behavior (SC-006); performer image must be rebuilt to ship performer-side changes (existing deploy step)
**Scale/Scope**: ~6 backend modules, 2 verdict sites (performer `main.py`, coordinare `monitor_performer.py`), env_cache service + state_store, http_performer_service; est. 10 bug fixes across 2 packages with ~25–35 tasks

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality First | PASS | No new dependencies; shared env-policy helper *removes* 4 divergent copies of PATH logic (single responsibility); type annotations on all new public surfaces. |
| II. Testing Discipline | PASS (plan enforces) | TDD mandatory for every behavior change: failing test → fix → green. Replay of the PR #159 payload becomes a permanent regression test (SC-001). Per-backend conformance tests for the shared env policy (SC-002). Coverage must not regress (CI gate ≥90% already enforced). |
| III. UX Consistency | PASS | User-facing surface is the QA PR comment and structured log events: comment must lead with blockers, never render dead links; error events carry actionable fields (what failed, why, what happens next). No GUI work. |
| IV. Performance by Design | PASS | Budgets: restart-resume < 2 min (SC-004); verdict gating O(criteria) in-process. Existing benchmark suite unaffected; no perf-critical path touched. |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers; ambiguous policy choices (hold vs fail vs advance-with-flag; budget default 3) were resolved in the spec's Assumptions with rationale. |

**Post-Phase-1 re-check**: PASS — design adds one new module (`_env_policy.py`), one new terminal status (`qa_env_blocked`), and four persisted fields; no constitution violations introduced. Complexity Tracking table not needed.

## Project Structure

### Documentation (this feature)

```text
specs/088-qa-verdict-integrity/
├── spec.md              # Feature specification (done)
├── plan.md              # This file
├── research.md          # Phase 0: decisions + rationale per bug
├── data-model.md        # Phase 1: entities, fields, state transitions
├── quickstart.md        # Phase 1: how to validate the feature end-to-end
├── contracts/
│   ├── qa-evidence-result.md     # QA result JSON field registry (performer → coordinare)
│   └── env-cache-state.md        # Persisted EnvCacheState field registry (state_store)
└── tasks.md             # Phase 2 (/speckit.tasks — not created by /speckit.plan)
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── main.py                      # A1/A2: _qa_unsubstantiated_pass + qa verdict assembly (~810-847, ~2270-2450)
│                                # A5: visual-evidence upload validation before PR-comment render (~2345-2394)
│                                # A4: app-boot evidence gate (qa output handling + prompt)
└── backends/
    ├── _env_policy.py           # NEW (B1): shared build_subprocess_env() — image PATH first, cache appended
    ├── claude_code.py           # B1: replace inline append logic with _env_policy
    ├── openclaw.py / opencode.py / opencode_compat.py / codex.py / pi.py / hermes.py / junie.py
    │                            # B1: adopt _env_policy (fixes hermes/junie full-PATH bugs)

src/coordinare/
├── graph/nodes/monitor_performer.py  # A3: terminal-verdict branch consults env_cache_health_failed (~2031)
│                                     # A1: handle new qa_env_blocked terminal status (hold + structured reason)
├── services/env_cache.py             # B2: attempt budget + escalating cooldown + bootstrap_exhausted
│                                     # B3: restart-resume via clean verify (skip full bootstrap)
├── services/http_performer_service.py # B4: secret-refresh retry-once-then-degrade; B5: malformed-JSON logging
├── state_store.py                    # B2/B3: persist attempts, exhausted flag, succeeded SHA
└── daemon.py                         # B3: startup clean-verify path wiring (reuses _verify_env_cache_clean)

tests/
├── unit/test_060_env_cache.py        # B2/B3 state machine + persistence tests
├── unit/graph/nodes/test_monitor_performer*.py  # A3 + qa_env_blocked handling
├── unit/services/test_http_performer_service.py # B4/B5
agent/performer/tests/unit/
├── backends/test_env_policy.py       # NEW: shared policy tests
├── backends/test_<each backend>.py   # B1 conformance tests (6 backends)
└── test_qa_verdict.py / main tests   # A1/A2/A4/A5 incl. PR #159 replay fixture
```

**Structure Decision**: Existing two-package layout unchanged. One new module (`backends/_env_policy.py`) consolidates env merging; everything else is surgical edits at the audited file:line sites. Performer-side changes require the standard image rebuild to deploy.

## Complexity Tracking

No constitution violations to justify — table omitted.
