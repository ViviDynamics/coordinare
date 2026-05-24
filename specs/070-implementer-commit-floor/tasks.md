---
description: "Task list for spec 070 — Implementer Commit-or-Checkpoint Contract (retroactive)"
---

# Tasks: Implementer Commit-or-Checkpoint Contract

**Status**: LANDED. All tasks marked `[X]`; this file documents what
shipped under commits `f83fb1c`, `92509f6`, `8262291`, `b735967`.

**Input**: Design documents from `/specs/070-implementer-commit-floor/`

---

## Phase 1: Protocol surface

- [X] T001 Add `partial_progress` to `ProtocolResponse.status` enum in `src/coordinare/protocol/models.py` and `agent/performer/src/performer/protocol.py`.
- [X] T002 Add `head_before: str | None`, `head_after: str | None`, `next_focus: str | None` to both `ProtocolResponse` models.
- [X] T003 Update protocol contract test in `tests/unit/protocol/test_protocol_contract.py` to include `partial_progress` in the valid-status assertion (commit `8262291`).

## Phase 2: Performer wiring

- [X] T004 In `agent/performer/src/performer/main.py`, capture branch HEAD at dispatch start; surface it as `head_before` on every terminal response.
- [X] T005 After implementer turn, recapture HEAD; surface as `head_after`.
- [X] T006 Parse trailing `{"status": "partial_progress", "next_focus": "..."}` JSON sentinel in implementer output. Gate strictly on `role == implementing`.
- [X] T007 On `partial_progress`: push commits, post a PR comment relaying `next_focus`, return `partial_progress` with `head_before`/`head_after`/`next_focus` populated.
- [X] T008 On `pr_opened` and `blocked` terminal responses, surface head deltas so the coordinare guardrail (Phase 3) can compare.

## Phase 3: Coordinare wiring

- [X] T009 In `src/coordinare/graph/nodes/monitor_performer.py`, add a `partial_progress` branch that relays `next_focus` into the next dispatch directive and re-routes to `dispatching` (stage `implementing`).
- [X] T010 In `src/coordinare/graph/nodes/monitor_performer.py`, add the zero-commit guardrail: when `stage == implementing` AND incoming verdict is `blocked` AND `head_before == head_after`, route back to `dispatching` with a stronger directive instead of honoring the verdict. Gated on `stage == implementing` so reviewer/security/qa/docs blocks are unchanged.
- [X] T011 Add tests in `tests/unit/graph/nodes/test_monitor_performer.py` for: partial_progress relay, zero-commit guardrail trips for implementer, guardrail does NOT trip for reviewer/security/qa/docs.

## Phase 4: Persona restructure (commit `92509f6`)

- [X] T012 Rewrite every default persona in `DEFAULT_INSTRUCTIONS` (`src/coordinare/services/persona_service.py`) to a consistent Role / Process / Output / Forbidden skeleton with markdown headings and fenced JSON contracts.
- [X] T013 Preserve verbatim: all JSON field names; architect's `---TASKS---` separator; spliced `_CI_COMMITTER_DIRECTIVE` / `_CI_*` directives; QA tokens pinned by tests (`visual_evidence`, `criteria_passed`, etc.).
- [X] T014 Add "Implementation floor" to the implementer persona: zero-source-code DONE (only docs/cards/ commits) is a critical failure; DONE definition requires the PR diff to actually contain the implementation.
- [X] T015 [P] Run pinned-token tests for all personas — assert no regression on field names or required output keys.

## Phase 5: Polish

- [X] T016 [P] (commit `b735967`) Replace en-dashes with hyphens across persona strings to satisfy `RUF001`.
- [X] T017 [P] `.venv/bin/ruff check` clean on touched files.
- [X] T018 [P] Full `.venv/bin/pytest tests/unit/` green.

---

## Manual Validation

- Reproduce card #70 / PR #135: implementer emits "next steps" prose
  without commits → coordinare MUST re-dispatch (not emit a blocked
  notification).
- Reviewer/security/qa contract tests still route `blocked` → `phase=blocked`.
- Operator-facing PR comment on `partial_progress` is human-readable
  and includes the relayed `next_focus`.
