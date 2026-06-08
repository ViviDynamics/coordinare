# Implementation Plan: Security Scan Gate

**Branch**: `083-security-scan-gate` | **Date**: 2026-06-08 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/083-security-scan-gate/spec.md`

## Summary

Make the `security` performer verdict no longer purely model judgment by adding a
deterministic static-analysis floor (semgrep + bandit) plus a CWE taint→sink prompt
restructure and a load-time config denylist. The coordinare scans the PR diff **once** at
dispatch, stashes findings in transient graph state, injects them into `card_context` (the
model's advisory ceiling), and at verdict time **forces `security_failed`** if any stashed
finding is critical/high — regardless of the model's `passed` value. The gate is
**fail-closed**: scanner/diff-fetch failures emit `security_failed` with a synthetic
critical `scanner_unavailable` finding routed to halt/human attention. Acceptance bar: the
082 vulnerable bench PR re-run yields `security_failed`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + finding models, reused unchanged), langgraph
(dispatch/monitor graph nodes), structlog (observability), semgrep (`--config auto --json`)
and bandit (`-f json`) invoked via subprocess, httpx/`gh` for diff acquisition
**Storage**: N/A — no new persisted state. Scanner findings live in transient graph state
(`state["scanner_findings"]`), mirroring the `relay_feedback` lifecycle.
**Testing**: pytest (`.venv/bin/pytest`), ruff lint (`.venv/bin/ruff check`)
**Target Platform**: Linux server (coordinare host) + performer Docker images (`Dockerfile.full`)
**Project Type**: single (coordinare service + performer image)
**Performance Goals**: scan completes within the existing security-dispatch budget; a single
scan per security dispatch (run-once, reuse) — no double coordinare scan.
**Constraints**: Fail-closed on any scanner/diff failure. Secrets discipline: no raw diff
text and no `auth_env`-resolved values in INFO logs; INFO limited to scan summary
(counts/severities). No protocol schema change (reuse `ProtocolResponse.findings` and the
`security_passed`/`security_failed` enum).
**Scale/Scope**: One new service (`security_scanner.py`), one GitHub helper extension
(`get_pr_diff`), two graph-node integrations (dispatch + monitor), one persona rewrite, one
config validator, plus the `Dockerfile.full` tool install. ~6 source touch-points.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. New `security_scanner.py` is a single-responsibility,
  pure/deterministic module (`scan_diff(...) -> list[Finding]`). Reuses existing finding
  schema and status enum; no schema churn. Follows the established service/graph-node split.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD RED-first throughout: scanner unit
  tests (fixture diffs: injection→critical, clean→empty, semgrep-only, bandit-only, malformed
  output), monitor override tests (082 regression, medium/low no-override, fail-closed), persona
  assertion test, config denylist tests. Coverage MUST NOT decrease; all tests deterministic
  (fixture-based, no live network — scanner subprocess mocked at the boundary).
- **III. UX Consistency** — N/A (no dashboard/UI surface change). Observability markers follow
  existing `monitor_performer` marker conventions.
- **IV. Performance by Design** — PASS. Run-once-reuse (FR-006) is the budget guard; the scan
  executes a single time at dispatch and is reused for prompt-injection and verdict floor.
- **V. Clarity Before Action** — PASS. No NEEDS CLARIFICATION remain (see Phase 0). Fail-closed
  behavior, denylist contents, and finding-routing for `scanner_unavailable` are all explicit
  in the spec.

**Security/secrets gates (project-specific, from 073/080/081 discipline):** no secret values or
raw diff text in logs (FR-011); `auth_env` is a name, never a value. Satisfied by INFO-summary-only
logging.

No constitution violations → Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/083-security-scan-gate/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature spec (already written)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   └── security_scanner.md  # scan_diff + get_pr_diff contracts
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   ├── security_scanner.py      # NEW (P1): scan_diff(changed_files, repo_root) -> list[Finding]
│   │                            #          wraps semgrep + bandit, normalizes to finding schema
│   ├── persona_service.py       # MODIFY (P2): security persona (~:326) → CWE taint→sink checklist
│   └── github.py                # MODIFY (P1): add get_pr_diff(pr_url) -> (raw_diff, changed_files)
├── graph/nodes/
│   ├── dispatch_performer.py    # MODIFY (P1): when role=="security", scan diff once,
│   │                            #          stash state["scanner_findings"], inject card_context block
│   └── monitor_performer.py     # MODIFY (P1): floor enforcement (~:2196) — critical/high
│   │                            #          stashed findings force security_failed + merge relay_feedback
├── protocol.py                  # REUSE only: ProtocolResponse.findings (:61), status enum (:20-21)
└── config.py                    # MODIFY (P3): load-time validator — security role denylist

agent/performer/
└── Dockerfile.full              # MODIFY (P1): install semgrep + bandit (ruff/black/shellcheck layer);
                                 #          expose as advisory performer tool (NOT verdict-binding).
                                 #          Dockerfile.base source-COPY invariant UNCHANGED.

tests/unit/
├── services/test_security_scanner.py   # NEW (P1): fixture diffs, normalization, malformed output
├── graph/nodes/test_monitor_performer.py # MODIFY (P1): override / no-override / fail-closed
├── test_persona_service.py             # MODIFY (P2): persona contains CWE checklist structure
└── test_config*.py                     # MODIFY (P3): denylist load-time validation
```

**Structure Decision**: Single-project coordinare layout (existing). P1 lands the load-bearing
floor (new `security_scanner.py` service + `github.get_pr_diff` helper + dispatch/monitor
integration + `Dockerfile.full` tool). P2 is a prompt-only change in `persona_service.py`. P3 is a
load-time validator in `config.py`. Tests mirror source under `tests/unit/`. No new top-level
directories; all paths are existing coordinare modules.

## Complexity Tracking

> No constitution violations — section intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
