# Tasks — Baseline Repair Autonomy (spec-090)

**Feature**: Baseline Repair Autonomy
**Branch**: `090-baseline-repair-autonomy`
**Inputs**: [plan.md](plan.md), [spec.md](spec.md), [data-model.md](data-model.md),
[research.md](research.md), [contracts/repair-dispatch.md](contracts/repair-dispatch.md),
[contracts/gate-decision.md](contracts/gate-decision.md), [quickstart.md](quickstart.md)

**Tests are REQUIRED** (Constitution II Testing Discipline is NON-NEGOTIABLE; SC-003 /
SC-004 mandate adversarial corpora; SC-006 / SC-009 mandate defaults-inert + migration
tests). Every implementation task is preceded by its test task (TDD).

Run tests with `.venv/bin/pytest`; lint with `.venv/bin/ruff check <files>`.

## Phased rollout (matches plan.md)

The three layers are independently shippable and **default-disabled**. With nothing
enabled, coordinare behaves byte-identically to pre-spec-090 (SC-006). Ship order:
**US1 (L1, MVP) → US2 (L2, observe-only) → US3 (L3, opt-in)**.

## Format

`- [ ] [TaskID] [P?] [Story?] Description with file path` — `[P]` = parallelizable
(different files, no incomplete dependency); story label on user-story phases only.

---

## Phase 1: Setup

- [X] T001 Confirm the baseline test suite is green on the branch and capture the
  pre-feature reference for SC-006: run `.venv/bin/pytest -q`,
  `.venv/bin/ruff check src/coordinare tests`, and
  `.venv/bin/pytest --cov=coordinare --cov-report=term-missing` — recording the pass state
  **and the baseline coverage percentage** (the `fail_under=90` gate in `pyproject.toml`)
  before any spec-090 code lands, so T038 can prove coverage did not decrease.

---

## Phase 2: Foundational (blocking — MUST complete before any user story)

Shared model + transport + state + config changes every layer depends on. All new
fields default to pre-feature values (SC-006).

### F1 — `CheckEntry` failure text + GraphQL `output{}` (data-model §2; feeds FR-007)

- [X] T002 [P] In `tests/unit/services/test_pr_checks_service.py`, add tests asserting:
  `CheckEntry` constructs with `title`/`summary` defaulting to `None` (frozen preserved);
  `parse_rollup` populates `title`/`summary` from a CheckRun `output{title,summary}`
  block; and legacy / `StatusContext` rows (no `output`) yield `None` for both.
- [X] T003 In `src/coordinare/services/pr_checks_service.py`, add `title: str | None = None`
  and `summary: str | None = None` to `CheckEntry`; extend the `_ROLLUP_CORE` CheckRun
  fragment with `output { title summary }`; populate the fields in `parse_rollup`; update
  the rollup fixtures used by the tests above.

### F2 — base-branch rollup fetch + `rollup_origin` + FR-027 operator signal (data-model §3; FR-005, FR-027)

- [X] T004 [P] In `tests/unit/services/test_pr_checks_service.py`, add tests asserting:
  `CheckRollup` carries `rollup_origin` defaulting to `"head"` (frozen preserved);
  `parse_base_rollup` stamps `rollup_origin="base"` with neutral `pr_number=0` /
  `head_pushed_at=None`; `get_base_branch_check_rollup` returns `None` (never raises) on
  fetch error / timeout / ambiguous base (FR-005 fail-safe); and the FR-027 degraded
  tracker emits a structlog **error** `baseline_fetch_degraded` only after ≥5 fetch
  failures within a 1-hour window for a repo (deterministic monotonic clock injected),
  with explicit boundary assertions — 4 failures → **no** signal, 5 → signal, 6 → signal —
  and failures older than the 1-hour window aging out so they stop counting.
- [X] T005 In `src/coordinare/services/pr_checks_service.py`, add
  `rollup_origin: Literal["head","base"] = "head"` to `CheckRollup`; add a
  `_BASE_ROLLUP_QUERY` (reusing the F1 fragment), `parse_base_rollup`, and
  `get_base_branch_check_rollup(...) -> CheckRollup | None` (fail-safe `None` on any
  error); and add the FR-027 `baseline_fetch_degraded` failure tracker (per-repo deque +
  injected monotonic clock, threshold ≥5/1h) invoked on each fetch failure.

### F3 — three gate-config classes (data-model §10; FR-015, FR-025, SC-006)

- [X] T006 [P] In `tests/unit/test_config.py`, add tests asserting: a `PersonaScopeConfig`
  built from a block that omits all three gate keys validates and exposes
  `baseline_prevention_gate.enabled is False`, `baseline_classification_gate.enabled is
  False`, `inherited_repair_gate.enabled is False`,
  `inherited_repair_gate.max_repair_attempts_per_head == 1`; each class rejects unknown
  keys (`extra="forbid"`); and `max_repair_attempts_per_head` enforces `ge=0, le=20`
  (0 and 20 accepted, -1 and 21 rejected).
- [X] T007 In `src/coordinare/config.py`, add `BaselinePreventionGateConfig`,
  `BaselineClassificationGateConfig`, and `InheritedRepairGateConfig`
  (`max_repair_attempts_per_head: int = Field(default=1, ge=0, le=20)`), each with
  `ConfigDict(extra="forbid")` and `enabled: bool = False`; nest all three on
  `PersonaScopeConfig` after `local_test_gate` via `Field(default_factory=...)`.

### F4 — state schema v8 → v9 migration (data-model §9; FR-026, SC-009)

- [X] T008 [P] Create `tests/contract/test_state_persistence_v8_to_v9.py` (mirroring
  `tests/contract/test_state_persistence_v7.py`): assert `CURRENT_SCHEMA_VERSION == 9`,
  `MIN_SUPPORTED_SCHEMA_VERSION` unchanged; a synthetic v8 snapshot (no
  `inheritance_repair_counter`, no `repair_audit`) loads cleanly and migrates to v9 with
  `inheritance_repair_counter == {}` **and** `repair_audit == []`; an empty counter means
  **zero attempts taken** (not unlimited); and a fresh v9 session — including a
  `repair_audit` populated with `RepairDecisionRecord`s — round-trips through
  `model_dump(mode="json")` → `model_validate`.
- [X] T009 In `tests/contract/test_state_persistence.py`, update the schema-version
  assertion from `8` to `9`.
- [X] T010 In `src/coordinare/state_store.py`, add the frozen `RepairDecisionRecord`
  model (`extra="forbid"`; fields `head_sha`, `attempt`, `kind` ∈ {dispatch,
  static_guard, reviewer, acceptance, rejection, escalation}, `is_safe: bool | None`,
  `flagged_patterns: list[str]`, `detail: str | None`, `decided_at: str`); add both
  `inheritance_repair_counter: dict[str, int] = Field(default_factory=dict)` **and**
  `repair_audit: list[RepairDecisionRecord] = Field(default_factory=list)` to
  `PersistedSession` immediately after `local_fix_counter` (data-model §9, FR-023); bump
  `CURRENT_SCHEMA_VERSION` 8 → 9; add the v9 line to the migration comment block.

---

## Phase 3: User Story 1 — Refuse to merge onto a red base (L1, P1) 🎯 MVP

**Goal**: at the closer/merge boundary, hold (re-evaluating) an approved, head-green PR
while a *required* base-branch check is red; fall safe on an indeterminate base; never
latch. **Independent test** (spec US1): red required base → no merge + base-not-green hold
recording the offending base check name/URL; base turns green → merge proceeds;
unfetchable base → today's head-only behavior; non-required base failure → no block.

- [X] T011 [P] [US1] In `tests/unit/services/test_base_gate.py` (new), test
  `evaluate_base_gate` returns a `BaseGateDecision` (`decision` ∈ {BLOCK, PROCEED,
  INDETERMINATE}; `failing_checks` lists the offending base check(s)): a base rollup with a
  **required** check in `failure` → BLOCK carrying that check's name + URL (FR-002, FR-003);
  only **non-required** base failures → PROCEED (FR-004); `None` base rollup → INDETERMINATE
  (fall-through), never BLOCK (FR-005, SC-002); re-evaluation re-reads and is not latched
  (FR-006).
- [X] T012 [US1] Create `src/coordinare/services/base_gate.py` defining `BaseGateDecision`
  (frozen pydantic model: `decision: Literal["BLOCK","PROCEED","INDETERMINATE"]`,
  `failing_checks: list[FailedCheck] = Field(default_factory=list)`, `extra="forbid"`) and
  `evaluate_base_gate(base_rollup, scope, ...) -> BaseGateDecision` that resolves the base
  required set via `required_checks_resolver.resolve` and decides via
  `pr_checks_policy.decide` over the base rollup, returning a structured base-not-green hold
  (BLOCK) / PROCEED / INDETERMINATE result naming the offending base check(s) (FR-001–FR-006;
  see data-model §11).
- [X] T013 [P] [US1] In `tests/unit/graph/nodes/test_monitor_pr.py`, add tests for the L1
  wiring: when `baseline_prevention_gate.enabled` and `evaluate_base_gate` returns BLOCK,
  the node holds (does not advance to `merging`) and records the base-not-green hold with
  the base check name + URL distinct from head failures (FR-002, FR-003); BLOCK then
  PROCEED across cycles proves no-latch (FR-006); INDETERMINATE → proceeds with head-only
  behavior (FR-005); gate disabled → behavior byte-identical to today (SC-006); only
  non-required base failing → not blocked (FR-004).
- [X] T014 [US1] In `src/coordinare/graph/nodes/monitor_pr.py`, wire the L1 gate between the
  `approved == True` precondition and the `state["phase"] = "merging"` transition: when
  `baseline_prevention_gate.enabled`, fetch the base rollup
  (`get_base_branch_check_rollup`), call `evaluate_base_gate`, and on BLOCK hold +
  re-evaluate (record the structured base-not-green hold); on INDETERMINATE/disabled fall
  through to the existing merge path (FR-001–FR-006).
- [X] T015 [US1] In `tests/integration/test_065_ci_gate.py` (or a new
  `tests/integration/test_090_base_gate.py`), add an end-to-end L1 scenario covering
  SC-001 (no merge while required base red, merge proceeds once green) and SC-002
  (indeterminate base → identical-to-baseline merge behavior).

**Checkpoint**: L1 is independently shippable and valuable with zero autonomy.

---

## Phase 4: User Story 2 — Classify a failure's origin (L2, P1, observe-only)

**Goal**: label each failing head check INHERITED / INTRODUCED / FLAKE / UNKNOWN by
comparing reason-sensitive signatures against the merge-base baseline, recording the
labels on the gate decision and observability **without changing any verdict/routing**.
**Independent test** (spec US2): same-reason stable baseline match → INHERITED;
new/previously-green → INTRODUCED; same name, *different* reason → INTRODUCED
(anti-masking); transient baseline counterpart → not INHERITED; unfetchable baseline →
all UNKNOWN; verdict/routing unchanged; pre-feature snapshot loads with new fields
defaulted.

### Failure signature (data-model §1; FR-007, SC-003)

- [X] T016 [P] [US2] Create `tests/unit/services/test_failure_signature.py` with a
  **normalization corpus** and an **anti-masking corpus**: `normalize_reason` is title-
  primary / summary-fallback / `""`-when-absent, lowercases and collapses whitespace, and
  strips drift (ISO timestamps→`<ts>`, durations→`<dur>`, run/job IDs→`<id>`, 16+hex/
  SHA→`<hex>`, UUID→`<uuid>`, hash-paths→`<path>`, line/col→`:<n>`); identical root cause
  with drift → identical signature; genuinely different reason → different signature;
  `make_failure_signature` hash is exactly 16 hex chars; pure/deterministic (no clock/RNG).
- [X] T017 [US2] Create `src/coordinare/services/failure_signature.py` with
  `normalize_reason(title, summary) -> str` (module-load-compiled drift regexes) and
  `make_failure_signature(name, conclusion, title, summary) -> tuple[str, str]` returning
  `(sha256(f"{name}\x1f{conclusion}\x1f{normalized}").hexdigest()[:16], normalized)`
  (FR-007).

### Signature-bearing failed check + collision detection (data-model §4; gate-decision.md)

- [X] T018 [P] [US2] Create `tests/unit/services/test_ci_gate.py` testing
  `FailedCheckWithSignature` (subclass of `FailedCheck`, adds `head_signature: str` and
  `baseline_signature: str | None = None`, `extra="forbid"` preserved) and
  `compare_signatures`: equal 16-char strings → match; two distinct normalized reasons
  hashing to the same 16-char value → collision detected (routes to UNKNOWN, never
  INHERITED).
- [X] T019 [US2] In `src/coordinare/services/ci_gate.py`, add `FailedCheckWithSignature`
  and `compare_signatures(...)` (byte-equal head/baseline signature match with collision
  detection) per data-model §4 and gate-decision.md.

### Classification (data-model §5; FR-008–FR-012, SC-003)

- [X] T020 [P] [US2] Create `tests/unit/services/test_failure_classification.py` covering
  the full source-order decision table and asserting the Stable=`{"failure"}` vs Transient
  boundary per data-model§5 (7 conclusions: timed_out, cancelled, neutral, skipped,
  action_required, stale, startup_failure): (1) transient head conclusion → FLAKE (FR-010);
  (2) no base rollup / collision → UNKNOWN (FR-012); (3) same-name baseline failure with a
  *transient* conclusion (flaky baseline) → INTRODUCED (FR-011); (4) stable baseline +
  matching signature → INHERITED (FR-008); (5) no baseline match, or stable baseline with
  a *different* signature → INTRODUCED (FR-009, anti-masking).
- [X] T021 [US2] Create `src/coordinare/services/failure_classification.py` with the
  `Classification` literal, the `BaselineFailure` value object, and
  `classify_failure_origin(head_check, baseline_index)` implementing the source-order
  table (FR-008–FR-012).

### Extended gate decision (data-model §6; gate-decision.md; FR-013, FR-014, SC-006)

- [X] T022 [US2] In `tests/unit/services/test_ci_gate.py`, add tests: `CIGateDecision`
  gains `inherited_checks` / `introduced_checks` (list[`FailedCheckWithSignature`]) and
  `flake_checks` / `unknown_checks` (list[`FailedCheck`]) all defaulting `[]`; the
  exactly-one-classification `model_validator` (mode="after") is a no-op when all four are
  empty (existing decisions stay valid — SC-006) and, when any is non-empty, requires
  every `failed_checks[].name` in exactly one list with no list naming an absent check;
  the validator never reads/mutates `verdict`; `_validate_verdict_invariants` still runs.
- [X] T023 [US2] In `src/coordinare/services/ci_gate.py`, add the four classification
  lists (`Field(default_factory=list)`) and the observe-only exactly-one-classification
  `model_validator` to `CIGateDecision`, leaving `verdict` and
  `_validate_verdict_invariants` untouched (FR-013, FR-014, SC-006).

### Wiring (FR-012, FR-013, FR-014, SC-006)

- [X] T024 [P] [US2] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add
  tests: with `baseline_classification_gate.enabled`, `_evaluate_ci_gate` builds a
  baseline index from the base rollup, classifies each failed head check, and attaches the
  four lists to the decision while the **verdict is byte-identical** to the gate-disabled
  path (FR-014, SC-006); an unfetchable base rollup → every head failure in
  `unknown_checks`, none INHERITED (FR-012); the labels are persisted on the decision and
  emitted to observability (FR-013); gate disabled → no lists populated, decision
  identical to today (SC-006).
- [X] T025 [US2] In `src/coordinare/graph/nodes/monitor_performer.py` `_evaluate_ci_gate`,
  when `baseline_classification_gate.enabled`, fetch the base rollup, build the
  `BaselineFailure` index, classify each failed head check via
  `classify_failure_origin`, and attach the four lists to the `CIGateDecision` — strictly
  observe-only (verdict/routing unchanged), persisted + emitted (FR-012, FR-013, FR-014).

**Checkpoint**: L2 explains every failure's origin; still changes no verdict (SC-006).

---

## Phase 5: User Story 3 — Guarded autonomous repair (L3, P2, opt-in)

**Goal**: for INHERITED stable failures, dispatch one bounded repair on the card's
existing branch with a do-not-weaken-tests mandate; gate every candidate diff through a
dual test-integrity guard (static + automated adversarial reviewer; either vetoes; uncertainty
rejects); land accepted repairs as candidates only (never auto-merge), relying on native
stale-approval dismissal; escalate visibly on veto / budget exhaustion. **Requires the L3
branch-protection prerequisite in [quickstart.md](quickstart.md).**

### Static test-integrity guard (data-model §8; FR-019, SC-004)

- [X] T026 [P] [US3] Create `tests/unit/services/test_test_integrity_guard.py` with the
  **test-weakening corpus**: `analyze_diff` returns `is_safe=False` (with a
  `flagged_patterns` reason) for each of (a) removed assertion, (b) added
  `@pytest.mark.skip`/`xfail`/disable, (c) loosened comparison/operator, (d)
  assertion wrapped in a swallowing `try/except` / made conditional, (e) mocked-away
  asserted requirement, and (f) removed setup/teardown/fixture; an unrecognizable /
  custom-assertion-framework diff it cannot confidently clear → `is_safe=False`
  (uncertainty rejects); and a clean code-only fix → `is_safe=True` (SC-004).
- [X] T027 [US3] Create `src/coordinare/services/test_integrity_guard.py` with
  `analyze_diff(diff) -> tuple[bool, list[str]]` (pure; conservative — cannot confidently
  clear ⇒ `False`) implementing the FR-019 static heuristics.

### Repair mandate payload (data-model §7; contracts/repair-dispatch.md; FR-016, FR-018)

- [X] T028 [P] [US3] In `tests/unit/graph/nodes/test_monitor_performer.py`, add tests that
  the built `metadata["repair_mandate"]` matches the repair-dispatch Field Registry:
  `type=="baseline_repair"`, non-empty `inherited_checks` each with `name` / `conclusion`
  (`"failure"`) / `normalized_reason` / `html_url`, `attempt` (1-based post-increment),
  `max_attempts` (== `max_repair_attempts_per_head`), and the durable `instruction`
  forbidding weaken/skip/xfail/delete/mock-away/loosen; assert **reason-fidelity** — each
  `normalized_reason` is byte-equal to
  `failure_signature.normalize_reason(head_check.title, head_check.summary)` (the canonical
  string the classifier hashed, **not** the raw title); absent when L3 disabled or no
  INHERITED in budget (SC-006, additive-only).
- [X] T029 [US3] In `src/coordinare/graph/nodes/monitor_performer.py`, construct
  `metadata["repair_mandate"]` from the INHERITED checks only when L3 is enabled, INHERITED
  is non-empty, and budget remains; populate each `normalized_reason` via
  `failure_signature.normalize_reason(check.title, check.summary)` on the head `CheckEntry`
  (the byte-identical canonical string the classifier hashed — not the raw title; safe
  because `normalize_reason` is pure) (FR-016, FR-017, FR-018; contracts/repair-dispatch.md
  reason-fidelity).
- [X] T030 [US3] In `src/coordinare/services/persona_service.py`, add the durable
  baseline-repair / do-not-weaken-tests mandate to `DEFAULT_INSTRUCTIONS['implementer']`
  after the spec-089 "Run the tests before you finish" block (FR-018).

### Dual guard land-or-escalate (data-model §8; FR-019, FR-020, FR-021, SC-005)

- [X] T031 [US3] Add tests in `tests/unit/graph/nodes/test_monitor_performer.py`: a clean diff passing **both**
  the static guard and the `diagnostic` adversarial reviewer lands as a **candidate** on
  the active branch and is **not** auto-merged, awaiting fresh human approval (FR-021,
  SC-005); a diff the static guard vetoes is rejected + escalated with no push (FR-020); a
  diff the static guard clears but the adversarial reviewer vetoes is rejected + escalated
  (FR-019, FR-020, either-vetoes); uncertainty from either half rejects.
- [X] T032 [US3] In `src/coordinare/graph/nodes/monitor_performer.py`, implement the dual
  guard on the candidate repair diff: run `analyze_diff`, dispatch the independent
  `diagnostic`-role adversarial reviewer (fresh context), and land-as-candidate only when
  **both** clear; either veto (or uncertainty) → reject, no push, escalate (FR-019,
  FR-020). Append a `static_guard` and a `reviewer` `RepairDecisionRecord` (carrying
  `is_safe` + any `flagged_patterns`) and then an `acceptance` (both cleared) or
  `rejection` (either vetoed, with the veto reason in `detail`) record to
  `PersistedSession.repair_audit`, mirroring each to observability (data-model §8/§9,
  FR-023). Never auto-merge; rely on native stale-approval dismissal and restate that
  assumption in the dispatch/escalation comment (FR-021, SC-005).

### Budget, escalation, audit (data-model §9; FR-022, FR-023, FR-024, SC-007, SC-008)

- [X] T033 [US3] Add tests in `tests/unit/graph/nodes/test_monitor_performer.py`:
  `inheritance_repair_counter[head_sha]` increments **at dispatch** and `attempt ≤
  max_attempts` is enforced before dispatch; with `max_repair_attempts_per_head: 1` a
  second INHERITED failure on the same head does **not** dispatch and instead sets
  `phase="blocked"` + an `open_questions` entry + a GitHub-comment escalation (FR-022,
  FR-024, SC-007); `max_repair_attempts_per_head: 0` classifies but never dispatches; a
  new head_sha starts a fresh budget; INTRODUCED / FLAKE / UNKNOWN never dispatch (FR-016);
  an indeterminate base (unfetchable rollup → all head failures UNKNOWN) never dispatches
  (FR-012 fail-safe); L3 disabled (default) → pure L1/L2 behavior (FR-015, SC-006); and
  `PersistedSession.repair_audit` accumulates the expected `RepairDecisionRecord`s in order
  — `dispatch`, then `static_guard` + `reviewer`, then `acceptance`/`rejection`, and an
  `escalation` record on budget exhaustion — each round-tripping through persistence
  (FR-023). Assert `repair_audit == []` for the L3-disabled run (SC-006).
- [X] T034 [US3] In `src/coordinare/graph/nodes/monitor_performer.py`, gate dispatch on
  budget (increment `inheritance_repair_counter[head_sha]` at dispatch, enforce `≤
  max_repair_attempts_per_head`), and on exhaustion / guard veto raise the visible
  escalation (`phase="blocked"` + `open_questions` + GitHub comment) instead of looping;
  restrict dispatch to INHERITED only and to enabled scopes. Append a `dispatch`
  `RepairDecisionRecord` at dispatch and an `escalation` record (reason in `detail`) on
  exhaustion to `PersistedSession.repair_audit`, mirroring each to observability — so the
  audit (with T032's guard/acceptance/rejection records) is the complete FR-023 trail
  (FR-015, FR-016, FR-022, FR-023, FR-024, SC-007).
- [X] T035 [US3] Add an integration test (`tests/integration/test_090_baseline_repair.py`,
  new) covering SC-005 (accepted repair reaches `main` only via a fresh post-repair human
  approval; coordinare never auto-merges) and SC-008 (a card blocked solely by an inherited,
  code-fixable required-check failure is resolved up to final approval with repair enabled).

**Checkpoint**: L3 delivers the governing intent — autonomous inherited-failure repair —
behind the dual guard, bounded, never auto-merged.

---

## Phase 6: Polish & cross-cutting

- [X] T036 [P] Create `tests/perf/test_090_gate_budgets.py` asserting the Constitution IV
  budgets: L1 enabled gate ≤500ms p95 (head+base fetched concurrently); classification
  ≤50ms p95 per rollup; `make_failure_signature` ≤50µs/check (≤5ms/rollup); static
  `analyze_diff` ≤100ms p95 on a 2000-line diff (research Decisions 1/2/3/5).
- [X] T037 [P] Execute [quickstart.md](quickstart.md) end-to-end: per-layer manual
  validation, the FR-027 operator-signal drill, the v8→v9 migration smoke (SC-009), and
  the **all-disabled baseline** check confirming routing/verdicts/merge decisions are
  byte-identical to a pre-spec-090 build (SC-006).
- [X] T038 [P] Verify Constitution II coverage did not decrease versus the T001 baseline:
  run `.venv/bin/pytest --cov=coordinare --cov-report=term-missing` (honoring the
  `fail_under=90` gate in `pyproject.toml`) and confirm the percentage is ≥ the T001
  figure; and confirm all new pure functions/modules (`failure_signature`,
  `failure_classification`, `test_integrity_guard`, `base_gate`, `ci_gate.compare_signatures`)
  are deterministic (no clock/RNG/network in unit paths).
- [X] T039 [P] Document the three gate configs (`baseline_prevention_gate`,
  `baseline_classification_gate`, `inherited_repair_gate` incl.
  `max_repair_attempts_per_head`) and the L3 branch-protection prerequisite in `AGENTS.md`
  / the config reference, restating the SC-006 defaults-off guarantee.
- [X] T040 Run `.venv/bin/ruff check src/coordinare tests` and the full
  `.venv/bin/pytest -q` suite; resolve any lint/test failures so the branch is green.

---

## Dependencies & execution order

- **Setup (T001)** → **Foundational (T002–T010)** → **User stories** → **Polish**.
- **Foundational blocks everything.** Within it: F1 (T002–T003) and F2 (T004–T005) both
  edit `pr_checks_service.py` → run sequentially (F1 then F2; the base query reuses F1's
  fragment). F3 (T006–T007) and F4 (T008–T010) are independent of F1/F2 and of each other
  → may run in parallel with the pr_checks work.
- **US1 (T011–T015)** depends on F2 (base rollup fetch) + F3 (`baseline_prevention_gate`).
  It is the MVP and ships before US2/US3.
- **US2 (T016–T025)** depends on F1 (`title`/`summary`) + F2 (base rollup) + F3
  (`baseline_classification_gate`). Independent of US1 at the code level (different
  boundary: performer CI gate vs. closer merge gate).
- **US3 (T026–T035)** depends on US2 (it consumes `inherited_checks`) + F3
  (`inherited_repair_gate`) + F4 (`inheritance_repair_counter`).
- **TDD**: each `[P]` test task precedes its implementation task; the impl task is not
  `[P]` against its own test. Test tasks for different files within a phase are `[P]`.

## Parallel execution examples

- **Foundational kickoff**: T002 (F1 test), T004 (F2 test), T006 (F3 test), T008 (F4 test)
  can be drafted in parallel (different files), then their impls land respecting the
  F1-before-F2 file-order rule.
- **US2 pure modules**: T016 (signature test), T018 (ci_gate test), T020 (classification
  test) are `[P]` — three independent new test files. T022 also edits `test_ci_gate.py`,
  so it follows T018 sequentially (not `[P]`).
- **L3 tests**: T026 (guard test) and T028 (mandate test) are `[P]` (different files);
  T031 and T033 also edit `test_monitor_performer.py`, so they follow T028 sequentially
  (not `[P]`).
- **Polish**: T036, T037, T038, T039 are `[P]` (perf / quickstart / coverage / docs).

## Implementation strategy

- **MVP = US1 (L1)** — highest-severity, lowest-complexity, zero autonomy. Shippable and
  valuable alone (no merges onto a known-red base).
- **US2 (L2) observe-only** — validate classification accuracy against real cards before
  any action; changes no verdict.
- **US3 (L3) opt-in** — autonomous repair last, gated/bounded, after L1/L2 are trusted.
- Every layer is config-gated and default-disabled; the all-disabled baseline (T037 /
  SC-006) is the headline guarantee of the phased rollout.
