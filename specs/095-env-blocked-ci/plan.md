# Implementation Plan: Infrastructure/Environment CI-Failure Classification (ENV_BLOCKED)

**Branch**: `095-env-blocked-ci` | **Date**: 2026-06-19 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/095-env-blocked-ci/spec.md`

## Summary

Add an `ENV_BLOCKED` classification, evaluated **before** the spec-090 inherited/introduced repair path, for CI failures that no code change can fix (artifact-storage quota, runner offline, billing/spending-limit, external-dependency outages in required jobs). An ENV_BLOCKED card is **held** (no autonomous repair, no performer re-dispatch), the specific infra cause + needed action is **surfaced once** to the operator, and normal flow **auto-resumes** when the signature clears. Detection is **signature-based and operator-extensible** (config-driven reason patterns) with a **fail-safe fallthrough** (unrecognized → existing 090 logic) and is **default-off**.

The feature extends the existing 090 classification substrate — `services/failure_classification.py` (`Classification` literal + `classify_failure_origin`), `services/failure_signature.py` (`normalize_reason`) — adds an env-pattern matcher, a per-card ENV_BLOCKED session state for dedup/auto-clear, a config gate (mirroring the 090 gates under `persona_scope`), and the hold/surface wiring at the existing classification/repair decision point in `graph/nodes/monitor_performer.py`. It reuses the existing operator notification channel.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + state models, reused), structlog (observability), the existing 090 classification (`failure_classification.py`, `failure_signature.py`) and CI-gate (`ci_gate.py`), the existing notify layer (`notify.py`), langgraph (unchanged). No new external dependencies.
**Storage**: Existing single-host single-process JSON snapshot via `state_store.py`. Adds a small per-card ENV_BLOCKED field on the persisted session (schema-version bump, backward-compatible default None/empty), parallel to 090's repair counters.
**Testing**: pytest (`.venv/bin/pytest`), unit tests in `tests/unit/` (classification, signature-matching, gate wiring, notification dedup), reusing existing 090 classification fixtures.
**Target Platform**: Linux/macOS coordinare daemon (single process).
**Project Type**: single (coordinare daemon).
**Performance Goals**: Per-card, per-evaluation pattern match over a small configured pattern list — negligible (string/regex match on already-fetched check reasons; no new I/O, reuses the rollup 090 already reads).
**Constraints**: Default-off (FR-012); fail-safe (unrecognized never ENV_BLOCKED, FR-003); secret-free state/logs/notifications (FR-010); evaluated on head alone, no baseline dependency (FR-011); dedup once per condition (FR-006); auto-clear on resume (FR-008).
**Scale/Scope**: Bounded by a card's required failing checks × configured patterns (single digits). Touches `failure_classification.py`, a new env-signature matcher, `config.py`, `state_store.py`, `monitor_performer.py`, `notify.py`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS. Extends the existing classification with one higher-priority branch; the env-matcher is a small pure function; config mirrors the established 090 gate pattern. No new dependency; type-annotated. Risk: piling logic into `classify_failure_origin` — mitigated by a separate `match_env_signature` pure helper.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS (planned). TDD. Unit tests: each built-in signature (quota/runner/billing) → ENV_BLOCKED; non-infra reason → fallthrough (no false ENV_BLOCKED, SC-005); ENV_BLOCKED → no repair mandate + no re-dispatch (SC-001); notification names cause+action and dedups (SC-002/SC-003); auto-clear on signature gone (SC-004/SC-008); disabled → identical to baseline (SC-006); the #159 quota scenario (SC-007); mixed env+introduced not masked (FR-009). Deterministic (no live GitHub). Coverage must not decrease.
- **III. User Experience Consistency**: PASS — **directly served**. Constitution III requires error messages to be *actionable* and not expose internals. ENV_BLOCKED's whole purpose is replacing a generic "tests failed" with a specific cause + operator action (FR-005), and the secret-free rule (FR-010) keeps internals out.
- **IV. Performance by Design**: PASS. No new I/O; pattern match on already-fetched reasons. Budget captured in SC (one-cycle resume, zero re-dispatch). No hot loop.
- **V. Clarity Before Action**: PASS. Spec Clarifications resolved the four design questions; no `NEEDS CLARIFICATION` markers. One implementation detail (exact placement of the env check relative to the flake/transient rows) is a Phase-0 research item with a decisive test, not an open ambiguity.

**Result**: PASS — no violations; Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/095-env-blocked-ci/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (classification + notification contract)
└── tasks.md             # Phase 2 output (/speckit.tasks — not created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   ├── failure_classification.py   # add "env_blocked" to Classification; evaluate env
│   │                               #   match BEFORE flake/inherited (highest priority).
│   ├── failure_signature.py        # reuse normalize_reason; env matcher keys on it.
│   └── env_signature.py            # NEW: match_env_signature(reason, patterns) -> EnvCause|None
│                                   #   built-in patterns (quota/runner/billing) + cause+action.
├── config.py                       # NEW EnvBlockedGateConfig under persona_scope (mirror 090
│                                   #   gates): enabled (default False) + operator pattern list.
├── state_store.py                  # per-card ENV_BLOCKED session field (schema bump, default None)
├── graph/nodes/monitor_performer.py# wire: ENV_BLOCKED → hold (no repair mandate, no re-dispatch),
│                                   #   notify once, auto-clear on resume.
└── notify.py                       # distinct env-blocked operator message (cause + action), deduped.

tests/unit/
├── services/test_env_signature.py            # NEW: pattern matching + fail-safe
├── services/test_failure_classification_env.py# NEW: env_blocked priority + fallthrough
└── graph/nodes/test_monitor_performer_env_blocked.py # NEW: hold/notify/resume wiring
```

**Structure Decision**: Single-project coordinare daemon. New `env_signature.py` service keeps the matcher single-purpose; classification/config/state/wiring extend existing 090 modules in place.

## Complexity Tracking

> No constitution violations — section intentionally empty.
