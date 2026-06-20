# Tasks: Junie Assessor Resilience

**Input**: Design documents from `/specs/098-junie-assessor-resilience/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/junie-resilience.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD, non-negotiable).

**Organization**: by user story (US1 P1 retry-not-block → US2 P2 normalize → US3 P3 ENV_BLOCKED+observability). Reuses the existing system-error retry budget, spec-095 ENV_BLOCKED, and the proxy normalizer chain. **No schema change, no new deps.**

## Path Conventions

Coordinare: `src/coordinare/`. Performer: `agent/performer/src/performer/`. Tests: `tests/` (coordinare) + `agent/performer/tests/` (performer proxy).

---

## Phase 1: Setup

- [X] T001 Confirm injection points by reading: `src/coordinare/graph/nodes/monitor_performer.py` (`marker=="error"` branch ~L3769-3787: `_FORMAT_ERROR_PREFIX` + `_is_transient_backend_error` → system-error retry; default block ~L3836), `src/coordinare/services/http_performer_service.py` (junie reason extraction ~L826), `agent/performer/src/performer/proxy/normalizers/` (strip_reasoning shape) + `proxy/launch.py` (`normalize`-mode `SelfHostedShim` `_launch_for_target`), `agent/performer/src/performer/backends/junie.py` (JUNIE_PROVIDER_BASE_URL usage), and the spec-095 ENV_BLOCKED classification. Note exactly where each US hooks.

---

## Phase 2: Foundational (failure-shape classifier — blocks US1 & US3)

- [X] T002 Write FAILING unit test `tests/unit/services/test_assessor_failure_shape.py` for a pure `classify_assessor_failure(reason: str) -> str | None` returning one of `empty_answer` / `malformed_body` / `empty_body` / `truncated`, else None: junie "Failed to build 'issue.md.junie_standalone'" → `malformed_body`; an empty-content/finish=length reason → `empty_answer`/`truncated`; an empty-response/timeout reason → `empty_body`; a normal prose/model-capability reason → None (must NOT be reclassified as retryable). MUST fail before T003.
- [X] T003 Implement `classify_assessor_failure(...)` (pure; signature-based, secret-free — matches on reason shape only) in a small module e.g. `src/coordinare/services/assessor_failure.py`. Make T002 pass.

**Checkpoint**: failure-shape classification is unit-tested in isolation.

---

## Phase 3: User Story 1 — Flaky response retried, not card-blocking (Priority: P1) 🎯 MVP

**Goal**: a junie parse/empty failure routes to the existing bounded system-error retry, not the default terminal block. **Independent test**: junie "Failed to build issue.md" → `phase=system_error` (retry), not `blocked`; clean → unchanged; blocks only after the budget.

- [X] T004 [US1] Write FAILING tests in `tests/unit/graph/nodes/test_monitor_performer_assessor.py`: (a) a junie assessor `marker=error` with reason "Failed to build 'issue.md.junie_standalone'" → routed to the system-error retry branch (`phase=system_error`, `system_error_count` incremented), NOT `phase=blocked`; (b) a clean assessor turn → unchanged (no retry); (c) after `system_error_count` reaches the budget, the card blocks (existing handle_system_error behavior). MUST fail before T005/T006.
- [X] T005 [US1] Tag the junie failure at the reason source in `src/coordinare/services/http_performer_service.py`: when the junie/assessor backend reports a parse/format/empty failure (via `classify_assessor_failure`), prefix the reason with `BACKEND_FORMAT_ERROR:` (malformed_body/empty_answer/truncated) so monitor's existing retry branch catches it. Make T004(a) pass.
- [X] T006 [US1] Defensive matcher: extend `_is_transient_backend_error` (or the error branch) in `src/coordinare/graph/nodes/monitor_performer.py` to also recognize an assessor-shape failure via `classify_assessor_failure`, so a junie failure routes to the system-error retry even if the source tag is missing. Make T004 pass.

**Checkpoint**: US1 independently testable — a single flaky assessor response retries instead of blocking; the MVP that fixes the live incident class.

---

## Phase 4: User Story 2 — Normalize junie's upstream (Priority: P2)

**Goal**: junie stops bypassing normalization; control-chars/empty-but-reasoned answers are repaired before its parser sees them. **Independent test**: control-char + empty-reasoned responses normalized clean; clean body unchanged.

- [X] T007 [US2] Write FAILING test `agent/performer/tests/unit/proxy/test_normalizer_control_chars.py`: a new normalizer strips invalid control bytes (`<0x20` except `\t\n\r`) from response string fields on BOTH the JSON and SSE paths; a clean body passes through unchanged (fail-open). MUST fail before T008.
- [X] T008 [US2] Implement the control-char-stripping normalizer in `agent/performer/src/performer/proxy/normalizers/` (mirror `strip_reasoning`'s JSON+SSE shape) and register it. Make T007 pass.
- [X] T009 [US2] Wire junie through a `normalize`-mode `SelfHostedShim`: in `proxy/launch.py` (`_launch_for_target`) include the junie backend in the normalize path with the control-char + strip_reasoning(+#130 promote) + envelope-complete chain; in `backends/junie.py` point `JUNIE_PROVIDER_BASE_URL` at the shim loopback. Test (`agent/performer/tests/unit/proxy/`) that a control-char / empty-reasoned upstream yields a clean, parseable response to junie, and a clean body is unchanged.

**Checkpoint**: US2 independently testable — most flaky responses become clean, so US1 retries rarely fire.

---

## Phase 5: User Story 3 — Persistent empty upstream → infrastructure + observability (Priority: P3)

**Goal**: exhausted empty-body failures surface as ENV_BLOCKED; every parse failure emits a shape-tagged secret-free record.

- [X] T010 [US3] Write FAILING tests in `tests/unit/graph/nodes/test_monitor_performer_assessor.py`: (a) when the system-error budget exhausts and the failures were `empty_body`, the card is surfaced as ENV_BLOCKED (spec-095) with an operator-actionable cause ("assessor model unavailable/overloaded"), NOT a generic block; (b) a malformed-body exhaustion blocks normally (not ENV_BLOCKED). MUST fail before T011.
- [X] T011 [US3] In `src/coordinare/graph/nodes/monitor_performer.py`, when the system-error path is about to block and the recorded assessor shape is `empty_body`, route through the spec-095 ENV_BLOCKED surfacing (reuse env_blocked state + notification) instead of the generic block. Make T010 pass.
- [X] T012 [US3] Emit a secret-free `assessor.parse_failure` record (shape + attempt; NO raw model output/tokens) at the classification site (monitor_performer / http_performer_service). Test (structlog capture) the record fields + absence of secrets (FR-004/FR-008).

---

## Phase 6: Polish & Cross-Cutting

- [X] T013 [P] Regression: run the monitor_performer + http_performer + system-error + 095 ENV_BLOCKED suites together; confirm reviewer/qa/security paths and a normal model-capability (prose) failure are unchanged (FR-006/FR-007/SC-005) — a prose failure must NOT be reclassified as infinitely retryable.
- [X] T014 [P] Performer proxy suite (`agent/performer/tests/unit/proxy/`) green incl. the new control-char normalizer; confirm no other backend's path changed.
- [X] T015 Walk `quickstart.md` A–F; confirm each SC (SC-001…SC-006) has a covering test; verify observability/state secret-free and no new external dependency.
- [X] T016 Full regression (`.venv/bin/pytest tests/ -q`; performer suite) + `.venv/bin/ruff check` on all edited files; fix nits.
- [X] T017 A couple of adversarial review rounds (diverse-lens finders + refute-verify) before merge — focus: prose-vs-flaky classification (don't make a real model-capability failure retry forever), retry boundedness/reset, empty-body→ENV_BLOCKED vs malformed→block, the shim wiring not breaking junie auth/profile, secret-free records, and other backends untouched.

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2 (classifier)** blocks US1 & US3.
- **US1 (P1)** = MVP (retry-not-block); fixes the incident class. Depends on Phase 2.
- **US2 (P2)** is performer-side, independent of US1; reduces how often US1 retries fire.
- **US3 (P3)** depends on US1 (acts at budget-exhaustion) + the classifier.
- **Polish** last.

## Parallel Opportunities

- US2 (performer proxy: T007/T008) parallel with US1 (coordinare: T004-T006) — different trees.
- T013 / T014 [P] (independent regression suites).

## Implementation Strategy

MVP-first: ship **US1** (tag flaky assessor failures as transient → existing bounded retry) — it alone stops the single-flaky-response card blocks (the live incident), reusing the system-error budget with no new state. Then **US2** (normalize junie's upstream so flaky responses become clean — fewer retries), then **US3** (empty-upstream → ENV_BLOCKED + shape observability). Each phase is an independently testable increment; the other backends and the happy path are unchanged.
