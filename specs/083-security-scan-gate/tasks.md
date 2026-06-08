# Tasks: Security Scan Gate

**Feature**: 083-security-scan-gate | **Branch**: `083-security-scan-gate`
**Input**: plan.md, spec.md, research.md, data-model.md, contracts/security_scanner.md, quickstart.md
**Tests**: REQUIRED — spec mandates TDD RED-first (Constitution II, NON-NEGOTIABLE).

## Conventions

- Run tests: `.venv/bin/pytest` (NOT `python -m pytest`). Lint: `.venv/bin/ruff check`.
- Each implementation task is preceded by its failing test (RED), then made GREEN, then committed.
- `[P]` = parallelizable (distinct files, no dependency on an incomplete task).
- Story labels: [US1] P1 floor, [US2] P2 persona, [US3] P3 config denylist.

---

## Phase 1: Setup

- [X] T001 Confirm semgrep + bandit are dev-installed for unit tests in `.venv` (`.venv/bin/pip install semgrep bandit` if absent); record versions in `specs/083-security-scan-gate/research.md` "Open items" if pinned. No source changes.
- [X] T002 [P] Create test fixture directory `tests/unit/services/fixtures/security_scanner/` with: `injection_diff/` (a file containing an obvious shell/SQL injection), `clean_diff/` (benign file), `semgrep_only/`, `bandit_only/` sample sources for `scan_diff` tests.

---

## Phase 2: Foundational (blocking prerequisites)

- [X] T003 Verify (read-only) the reused contracts are present and unchanged: `ProtocolResponse.findings` at `src/coordinare/protocol.py:61` and `security_passed`/`security_failed` enum at `src/coordinare/protocol.py:20-21`. Note exact current line numbers in a comment in `data-model.md` if they have drifted. No code change (FR-012 = no schema change).

---

## Phase 3: User Story 1 — Deterministic static-analysis floor (P1) 🎯 MVP

**Goal**: A critical/high static-analysis hit forces `security_failed` regardless of the model verdict; fail-closed on scanner/diff failure.
**Independent test**: Feed a fixture diff with a known injection through the gated security role with a model returning `passed:true`; coordinare overrides to `security_failed` and findings appear in `relay_feedback`.

### Scanner service (Contract 1)

- [X] T004 [P] [US1] RED: write `tests/unit/services/test_security_scanner.py` — cases: injection fixture → one critical/high finding (correct file/line/category); clean fixture → `[]`; semgrep-only fixture; bandit-only fixture; malformed tool output → raises `ScannerError`; tool-missing → raises `ScannerError`. Run, confirm FAIL (module not defined).
- [X] T005 [US1] GREEN: implement `src/coordinare/services/security_scanner.py` — `scan_diff(changed_files, repo_root) -> list[Finding]` wrapping semgrep (`--config auto --json`) + bandit (`-f json -r`), normalizing per data-model.md severity tables, default `routing="implementer"`; define `ScannerError`; "tool found issues" non-zero exit is NOT an error. INFO logs limited to scan summary (counts/severities), never raw diff (FR-011). Run T004 → PASS.
- [X] T006 [US1] Commit scanner service + tests.

### Diff acquisition (Contract 2)

- [X] T007 [US1] RED: add tests for `get_pr_diff` in the github service test module — mocked `gh pr diff` success → returns `(raw_diff, changed_files)`; fetch failure → raises. Run, confirm FAIL.
- [X] T008 [US1] GREEN: implement `get_pr_diff(pr_url) -> tuple[str, list[str]]` in `src/coordinare/services/github.py` (beside `get_pr_files`:989), using existing GH auth; raise on fetch failure; no token/diff at INFO. Run T007 → PASS.
- [X] T009 [US1] Commit github helper + tests.

### Dispatch integration (Contract 3)

- [X] T010 [US1] RED: add security cases to `tests/unit/graph/nodes/test_dispatch_performer.py` — security role → `scan_diff` called once + `state["scanner_findings"]` populated + `card_context` has `scanner_findings` block; non-security role → `scan_diff` NOT called; scan/diff error → fail-closed synthetic `critical`/`scanner_unavailable`/`routing:halt` finding stashed + observability marker. Run, confirm FAIL.
- [X] T011 [US1] GREEN: modify `src/coordinare/graph/nodes/dispatch_performer.py` (~:671 card_context build) — when `role == "security"`: `get_pr_diff` → `scan_diff` → stash `state["scanner_findings"]` → inject `scanner_findings` block into `card_context`; wrap in try/except for fail-closed synthetic finding + marker; scan exactly once. Run T010 → PASS.
- [X] T012 [US1] Commit dispatch integration + tests.

### Floor enforcement (Contract 4) — carries SC-001/SC-002/SC-003

- [X] T013 [US1] RED: add security floor cases to `tests/unit/graph/nodes/test_monitor_performer.py` — **082 regression**: scanner critical + model `passed:true` → overridden to `security_failed`; scanner medium/low + model pass → NOT overridden; `scanner_unavailable` → fail-closed `security_failed` routed to halt; clean scanner + model pass → `security_passed` stands; scanner findings merged into `relay_feedback`. Run, confirm FAIL.
- [X] T014 [US1] GREEN: modify `src/coordinare/graph/nodes/monitor_performer.py` (~:2196 verdict consumption) — before accepting `security_passed`, read `state["scanner_findings"]`; if any critical/high, force `security_failed` and merge findings into `relay_feedback`; reuse dispatch-stashed findings (no re-scan); `scanner_unavailable` routes to halt with loud marker. Run T013 → PASS.
- [X] T015 [US1] Commit floor enforcement + tests.

### Performer advisory tool (FR-007)

- [X] T016 [P] [US1] Modify `agent/performer/Dockerfile.full` — install semgrep + bandit in the ruff/black/shellcheck/eslint layer; expose as an advisory performer tool (feeds model `findings[]`, NOT verdict-binding). Do NOT touch `Dockerfile.base` (source-COPY invariant). Build base then full per quickstart to verify.
- [X] T017 [US1] Commit Dockerfile.full tool install.

**Checkpoint US1**: Floor is load-bearing and independently shippable. SC-001/002/003 testable now.

---

## Phase 4: User Story 2 — CWE taint→sink checklist (P2)

**Goal**: Security persona restructured into a CWE taint→sink checklist; output JSON contract unchanged.
**Independent test**: Rendered security persona contains sources/sinks/fixed-CWE-list structure AND still specifies `{passed, findings[]}` JSON.

- [X] T018 [US2] RED: add a case to `tests/unit/test_persona_service.py` asserting the rendered `security` persona contains the taint→sink checklist (untrusted sources → dangerous sinks → fixed CWE list: injection, broken authz, hardcoded secrets, insecure deserialization, path traversal, SSRF) AND still specifies the `{passed, findings[]}` JSON output. Run, confirm FAIL.
- [X] T019 [US2] GREEN: rewrite the `security` persona string in `src/coordinare/services/persona_service.py` (~:326) into the CWE taint→sink checklist, preserving the unchanged `{passed, findings[]}` JSON contract and the existing routing instruction (~:336). Run T018 → PASS.
- [X] T020 [US2] Commit persona rewrite + test.

**Checkpoint US2**: Independent of US1; model-agnostic ceiling raised.

---

## Phase 5: User Story 3 — Higher-capability judge gate (P3)

**Goal**: Config load rejects binding `security` to a known-weak model.
**Independent test**: Load config binding `security` to a denylisted model → validation error; capable model → no error; reviewer/assessor denylisted → no error.

- [X] T021 [US3] RED: add cases to `tests/unit/test_config*.py` — `security` → denylisted model (each of qwen3.6:35b, qwq:32b, qwen2.5:14b-instruct, qwen2.5:32b, qwen3-coder:30b) → raises at load; `security` → capable model → loads; reviewer/assessor → denylisted model → loads (out of scope). Mock `Path.cwd()`/home in discovery to avoid picking up repo `config.yaml`. Run, confirm FAIL.
- [X] T022 [US3] GREEN: add a load-time `model_validator` in `src/coordinare/config.py` resolving the `security` role model (reuse `resolved_role`:550) and raising on denylist membership; scope = `security` role only. Run T021 → PASS.
- [X] T023 [US3] Commit config denylist validator + tests.

**Checkpoint US3**: Independent of US1/US2; production lever codified.

---

## Phase 6: Polish & Cross-Cutting

- [X] T024 [P] Secrets-discipline assertion (SC-005): add/extend a test verifying NO raw diff content and NO `auth_env`-resolved values appear in INFO-level logs during a scan run (caplog at INFO); only scan summary present.
- [X] T025 Full-suite + lint gate: run `.venv/bin/pytest` and `.venv/bin/ruff check` over all touched files (quickstart command); confirm coverage does not decrease (Constitution II). Fix any regressions.
- [X] T026 Acceptance bar (SC-001): re-run the 082 vulnerable bench PR through the gated security role per quickstart; confirm `security_failed` with scanner findings in `relay_feedback`. Record the result in the PR description.
- [X] T027 Mark completed tasks `[X]` in this file; run `speckit.analyze` before declaring implementation complete (MEMORY.md mandate).

---

## Dependencies & Execution Order

- **Setup (T001–T002)** → **Foundational (T003)** → user stories.
- **US1 (T004–T017)** is the MVP and MUST land first — it carries SC-001/002/003 and is the load-bearing floor. Internal order: scanner → diff helper → dispatch → monitor → Dockerfile. T004→T005, T007→T008, T010→T011, T013→T014 are strict RED→GREEN pairs.
- **US2 (T018–T020)** and **US3 (T021–T023)** depend only on Setup/Foundational; independent of US1 and of each other — can be done in any order after US1, or in parallel by separate agents.
- **Polish (T024–T027)** after all stories.

### Parallel opportunities

- T002 (fixtures) ∥ T001.
- T016 (Dockerfile) ∥ the US1 Python tasks (distinct file).
- After US1: US2 and US3 are fully parallel (`persona_service.py` vs `config.py` + distinct test files).
- T024 ∥ other polish once sources exist.

## Implementation Strategy

MVP = **US1 alone** (deterministic floor). Ship/verify it against the 082 bench PR before
layering US2 (prompt) and US3 (config gate), which are small independent complements.

## Task count

27 tasks — Setup 2, Foundational 1, US1 14, US2 3, US3 3, Polish 4.
