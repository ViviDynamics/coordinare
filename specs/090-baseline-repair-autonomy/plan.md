# Implementation Plan: Baseline Repair Autonomy

**Branch**: `090-baseline-repair-autonomy` | **Date**: 2026-06-13 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/090-baseline-repair-autonomy/spec.md`

## Summary

Coordinare currently self-blocks when a card's PR shows a required-check failure that the
card did not introduce (inherited / out-of-scope baseline breakage), and it will merge a
card's PR even when the *base* branch is itself red — both behaviours make coordinare a
worse teammate than a careful human. This feature makes coordinare (1) **refuse to merge
onto a red base**, (2) **classify** each failing head check as INHERITED / INTRODUCED /
FLAKE / UNKNOWN by comparing it against the merge-base baseline using a *reason-sensitive*
failure signature, and (3) **autonomously repair** inherited breakage on the card's
existing branch behind a **dual test-integrity guard** that rejects any "fix" that weakens
or removes a test — landing the repair as a candidate for fresh human approval, never
auto-merging.

The technical approach extends the existing spec-064/075 CI machinery rather than
introducing a new subsystem:

- **Layer 1 (US1, P1)** adds a base-branch required-check read at the existing merge
  decision point (`monitor_pr.py`, just before `phase="merging"`). It introduces a new
  `get_base_branch_check_rollup()` on `pr_checks_service` (a *new* GraphQL query — the
  PR-rollup query is structurally incompatible with a `Ref` target) and reuses the
  spec-075 `pr_checks_policy.decide()` path against the base's *own* required set so base
  required failures BLOCK and non-required failures do not.
- **Layer 2 (US2, P1, observe-only)** adds a pure `failure_signature` /
  `failure_classification` service and four classification lists on `CIGateDecision`,
  populated in `monitor_performer._evaluate_ci_gate` immediately before `decide()`. It
  requires extending the coordinare GraphQL CheckRun fragment with an `output` block (the
  current fragment fetches no failure text) and `CheckEntry` with `title`/`summary`.
- **Layer 3 (US3, P2, opt-in)** reuses the spec-089 bounded self-fix loop in
  `monitor_performer`, keyed on a new per-head `inheritance_repair_counter`, dispatching a
  `repair_mandate` to the implementer, gating the resulting diff through a static
  `test_integrity_guard` plus an automated adversarial reviewer (a `diagnostic`-role
  performer dispatch), and escalating on any veto or budget exhaustion.

All three layers are config-gated and **default-disabled**; at defaults, routing, verdicts,
and merge decisions are byte-identical to the pre-feature baseline (SC-006). State migrates
forward at schema **v9** with safe empty defaults (an empty repair counter still enforces
the budget — it never means "unlimited").

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; production on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + decision/signature models), langgraph
(coordinare graph nodes — `monitor_pr`, `monitor_performer`, `routing`), structlog
(observability records), httpx (performer HTTP client; GitHub GraphQL via existing
`github.py` client), aiohttp (performer server side). GitHub Checks API via GraphQL
(`statusCheckRollup`, `branchProtectionRules`) on the coordinare side; REST `check-runs`
on the performer side (unchanged). No new external dependencies.
**Storage**: JSON snapshot via `state_store.py` (existing single-host single-process
store), extended with a per-head `inheritance_repair_counter: dict[str, int]` on
`PersistedSession`, parallel to spec-075's `bounce_counter` and spec-089's
`local_fix_counter`. Schema bumps `CURRENT_SCHEMA_VERSION` 8 → 9. No new store.
**Testing**: pytest via `.venv/bin/pytest`; ruff via `.venv/bin/ruff` (per project
convention — never `python -m pytest`). Contract tests under `tests/contract/`, unit
tests under `tests/unit/`.
**Target Platform**: Linux server (coordinare daemon) + Debian-based performer containers.
**Project Type**: single (one Python package `src/coordinare/`, plus the in-repo
`agent/performer/` package — unchanged by this feature except the implementer persona
instruction).
**Performance Goals**: Layer 1 base-rollup read MUST add ≤ 500 ms p95 to a merge-decision
cycle (head + base rollups fetched concurrently via `asyncio.gather`; base
branch-protection fetched in the same round trip where possible). Classification
(Layer 2) MUST add ≤ 50 ms p95 per gate evaluation over the pre-feature path (pure,
in-process signature computation over ≤ 100 checks). The Layer 3 adversarial-reviewer
dispatch is an out-of-band performer turn (30–90 s) and is NOT on the merge-decision hot
path — it runs only when repair is enabled and an INHERITED failure exists.
**Constraints**: At all flag defaults (disabled) the added code paths MUST be inert —
zero added GitHub round trips, zero added latency, identical decisions (SC-006). The
base-rollup fetch MUST fail safe: any error/timeout/indeterminate base → head-only
behaviour, never an indefinite block (FR-005, SC-002). The failure signature MUST be
deterministic and reason-sensitive; benign wording drift (timestamps, line numbers, run
IDs, paths-with-hashes, UUIDs) MUST NOT change it (FR-007).
**Scale/Scope**: Single coordinare daemon orchestrating O(10) concurrent cards; each PR
rollup is capped by GitHub at 100 check contexts. Repair attempt budget defaults to 1 per
head. Three new services, one new GraphQL query, four new `CIGateDecision` fields, three
new config classes, one new persisted counter, one new performer-dispatch payload field.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.1.0 (ratified 2026-02-16).

### Principle I — Code Quality & Maintainability ✅ PASS (with design discipline)

- **Single responsibility**: Each new unit has one job — `failure_signature.py`
  (normalize + hash), `failure_classification.py` (pure decision table),
  `test_integrity_guard.py` (static diff heuristic), a new
  `get_base_branch_check_rollup()` sibling on `pr_checks_service`. No god-object growth.
- **Interface-first / prefer duplication over interface bloat**: the base-rollup path
  does NOT overload the PR-rollup query (structurally incompatible — PR-specific
  `number`/`baseRefName`/`commits` fields are absent on a `Ref` target); a *separate*
  `_BASE_ROLLUP_QUERY` + `parse_base_rollup()` is the honest, duplication-tolerant design.
- **Type safety**: all new payloads are pydantic models (`FailedCheckWithSignature`,
  base-rollup DTO, three config classes); the classification decision table is a pure
  typed function. No `Any` on cross-boundary fields.
- **No dead code**: the design rejects the speculative `guard_lm_model` config knob from
  the design doc (not needed — the adversarial reviewer is a `diagnostic` dispatch, not an
  in-process LLM client). Layer 3 ships only when its config flag exists and is on.

### Principle II — Testing Discipline (NON-NEGOTIABLE) ✅ PASS — this principle is the feature's backbone

- **Coverage MUST NOT decrease**: this is *literally what Layer 3's dual guard enforces on
  the repository under repair* (FR-019, FR-020, SC-004) — the guard is the constitutional
  testing-discipline principle made executable. The guard's own static heuristic and the
  classification/signature logic are themselves covered by unit tests plus an **adversarial
  test-weakening corpus** (SC-004) and an **anti-masking corpus** (SC-003).
- **Deterministic tests**: the failure signature is deterministic by construction (FR-007);
  FLAKE-exclusion (FR-010, FR-011) keeps transient conclusions out of INHERITED so a flaky
  baseline can never anchor an auto-repair. A runtime **collision-detection** check
  (two distinct normalized reasons → same 16-char hash ⇒ escalate) prevents a silent
  signature collision from masking a regression.
- **Direct authority** for the FLAKE-exclusion rule and the guard. No constitutional
  tension — the feature strengthens testing discipline.

### Principle III — User Experience Consistency ✅ PASS

- Every escalation path produces a *visible, actionable* signal on the card/PR and a
  human-review/blocked phase (FR-024) — never log-only. Consistent with the existing
  blocked-card surfacing (spec 069) and CI-gate comment surfacing (spec 075). Reuses the
  existing notify/comment + phase machinery; no new UX vocabulary.

### Principle IV — Performance by Design ✅ PASS

- Performance budgets are stated in Technical Context **and** carried as a budget on each
  Phase 0 decision (per Constitution IV "measured not assumed"): Layer 1 ≤ 500 ms p95
  added per merge cycle (concurrent head+base fetch), Layer 2 ≤ 50 ms p95 added per
  evaluation (pure in-process), Layer 3 reviewer off the hot path. Research.md derives each
  budget and names the benchmark that proves it. At defaults, **zero** added cost.

### Principle V — Clarity Before Action ✅ PASS

- All spec clarifications are resolved (8 clarifications, Session 2026-06-13). This plan
  resolves every implementation-level NEEDS CLARIFICATION *here, now* — the concrete
  normalization regex set, the 16-char hash + collision heuristic, the base-rollup GraphQL
  shape, the `FailedCheckWithSignature` definition, the FR-027 systemic-failure mechanism,
  the approval-invalidation mechanism, the stable-vs-transient conclusion set, the config
  class structure, and the adversarial-reviewer dispatch mechanism — in Phase 0
  `research.md`. No NEEDS CLARIFICATION is deferred to implement time.

### Quality Gates (1–8) — applicability

1. Lint/Format (`.venv/bin/ruff`) — applies, gating.
2. Type Check — applies (pydantic models + typed pure functions).
3. Unit Tests — applies; new services + decision table + guard heuristic.
4. Integration Tests — applies; L1 merge-gate wiring, L2 observe-only invariance, L3
   dispatch+guard+escalation loop.
5. Coverage (MUST NOT decrease) — applies; the feature *adds* coverage and enforces it.
6. Performance — applies; the three budgets above are benchmarked.
7. Accessibility — N/A (no UI surface; dashboard already renders gate decisions).
8. Code Review — applies; binary human approval (no bot approvals), per project discipline.

**GATE RESULT: PASS.** No violations. Proceed to Phase 0. (Post-Phase-1 re-check below.)

## Project Structure

### Documentation (this feature)

```text
specs/090-baseline-repair-autonomy/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature specification (approved)
├── research.md          # Phase 0 output — 8 decisions, each with a perf budget
├── data-model.md        # Phase 1 output — entities + model deltas
├── quickstart.md        # Phase 1 output — per-layer manual validation
├── contracts/           # Phase 1 output — Field Registry + payload/decision shapes
│   ├── repair-dispatch.md       # coordinare → performer repair_mandate payload
│   └── gate-decision.md         # extended CIGateDecision serialization + classification
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

Single-project layout. Coordinare is one Python package; the performer is an in-repo sibling
package touched only for the implementer persona instruction (Layer 3). Real paths:

```text
src/coordinare/
├── services/
│   ├── failure_signature.py         # NEW (L2): normalize_reason + make_failure_signature (16-char SHA-256)
│   ├── failure_classification.py    # NEW (L2): pure decision table classify_failure_origin + helpers
│   ├── test_integrity_guard.py      # NEW (L3): static analyze_diff heuristic (conservative bias)
│   ├── ci_gate.py                   # EXTEND (L2): CIGateDecision +4 classification lists,
│   │                                #              FailedCheckWithSignature, compare_signatures()
│   ├── pr_checks_service.py         # EXTEND (L1/L2): _BASE_ROLLUP_QUERY + get_base_branch_check_rollup()
│   │                                #              + parse_base_rollup(); CheckEntry +title/+summary;
│   │                                #              CheckRun fragment +output{...}; systemic-failure deque (FR-027)
│   ├── pr_checks_policy.py          # REUSE (L1): decide() against base required set (075 path)
│   ├── required_checks_resolver.py  # REUSE (L1): sibling resolution for base required set
│   └── persona_service.py           # EXTEND (L3): implementer repair-mandate scope instruction
├── graph/
│   ├── nodes/
│   │   ├── monitor_pr.py            # EXTEND (L1): base-not-green hold before phase="merging"
│   │   └── monitor_performer.py     # EXTEND (L2/L3): classification insertion + repair self-fix loop
│   └── routing.py                   # REUSE: phase routing unchanged
├── config.py                        # EXTEND: BaselinePreventionGateConfig,
│                                    #         BaselineClassificationGateConfig,
│                                    #         InheritedRepairGateConfig (nested on PersonaScopeConfig)
└── state_store.py                   # EXTEND: CURRENT_SCHEMA_VERSION 8→9;
                                     #         inheritance_repair_counter on PersistedSession

agent/performer/src/performer/
└── (unchanged code paths; implementer reads repair_mandate from JobInitPayload.metadata)

tests/
├── contract/
│   ├── test_state_persistence.py            # EXTEND: schema_version 8→9 assertion
│   ├── test_state_persistence_v8_to_v9.py   # NEW: v8→v9 migration (mirror v7 template)
│   └── test_gate_decision_schema.py         # EXTEND: classification-list invariants
└── unit/
    ├── services/
    │   ├── test_failure_signature.py        # NEW: normalization + anti-masking corpus + collision
    │   ├── test_failure_classification.py   # NEW: decision-table source-order, first-match-wins
    │   ├── test_test_integrity_guard.py     # NEW: adversarial test-weakening corpus (SC-004)
    │   └── test_pr_checks_service.py        # EXTEND: base-rollup parse + fail-safe + output field
    └── graph/nodes/
        ├── test_monitor_pr.py               # EXTEND (L1): base-not-green hold + fail-safe
        └── test_monitor_performer_ci_gate.py# EXTEND (L2/L3): classification + repair loop + escalation
```

**Structure Decision**: Single project (Option 1). This feature is entirely within the
existing `src/coordinare/` package and its `tests/` tree, following the established
service-layer + graph-node + config + state-store layout. No new top-level project,
no web/mobile split. The only cross-package touch is the implementer persona instruction
string in `persona_service.py` (read by the performer at dispatch time via the existing
`JobInitPayload.metadata` channel — no performer code change).

## Complexity Tracking

> No constitutional violations require justification. This table records the *deliberate
> design tensions* the plan accepts and why the simpler alternative was rejected, for
> reviewer transparency.

| Decision | Why Needed | Simpler Alternative Rejected Because |
|----------|------------|--------------------------------------|
| Separate `_BASE_ROLLUP_QUERY` instead of reusing `_ROLLUP_CORE` | The PR-rollup fragment hard-codes PR-only fields (`number`, `baseRefName`, `commits`) that do not exist on a `Ref`/`Commit` target | Reusing `_ROLLUP_CORE` against a `reference(qualifiedName:)` target is a GraphQL type error — not a smaller change, an impossible one |
| New base branch-protection fetch + `decide()` via the 075 path | FR-002 requires base *required* failures to BLOCK; the default 064 path sets `branch_protection_readable=False → required=[]`, making base failures ADVISORY and silently breaking FR-002 | Reusing the head-side advisory default would pass tests but violate FR-002 in production — a correctness regression, not a simplification |
| Extend GraphQL CheckRun fragment with `output{title summary text}` | FR-007's reason-sensitive signature needs failure *text*; the current fragment fetches only `{name,status,conclusion,detailsUrl}` | Hashing only name+conclusion cannot satisfy the anti-masking property (FR-009) — same name+conclusion, different cause would falsely match as INHERITED |
| Three config classes (not one with nested L1/L2 flags) | Matches the established one-layer-per-class pattern (`CIGateConfig`, `LocalTestGateConfig`) and keeps each layer independently default-off for SC-006 | A single `InheritedRepairGateConfig` nesting L1/L2 flags couples the three independently-shippable layers and breaks the phased-rollout flag isolation |
| Adversarial reviewer as a `diagnostic`-role performer dispatch (not in-process LLM) | Gives the guard an independent *separate-context* judgment (FR-019) without adding an in-process model client dependency; FR-017 governs the repair *implementation* dispatch, not guard *evaluation*, so a diagnostic judge does not violate it | An in-process LLM client adds a new dependency + dead `guard_lm_model` config and shares context with no real isolation benefit |

---

## Phase 0 — Research

See [research.md](research.md). Eight decisions, each resolving the implementation-level
NEEDS CLARIFICATION with Decision / Rationale / Alternatives **and a measurable
performance budget** (Constitution IV). All adversarial corrections from the grounding
pass are folded in (no deferral to future docs — Constitution V).

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md), [contracts/](contracts/), and [quickstart.md](quickstart.md).
`contracts/` carries the **Field Registry** required by the speckit.analyze G-check for the
coordinare→performer repair-dispatch payload and the extended gate-decision serialization.

### Constitution Re-Check (post-Phase-1) ✅ PASS

After designing the data model and contracts, all five principles still hold:

- **I**: model deltas are minimal and typed; no interface bloat (base-rollup is a separate
  query/parse path; classification lists are additive `Field(default_factory=list)`).
- **II**: the data model adds an observe-only `exactly-one-classification` invariant on
  `CIGateDecision` (each failed check name appears in exactly one classification list) and
  a runtime collision-detection escalation — both strengthen determinism.
- **III**: no new UX surface; reuses existing comment/phase signalling.
- **IV**: every Phase 0 decision carries its budget; the data model adds no hot-path cost
  at defaults.
- **V**: zero NEEDS CLARIFICATION remain after Phase 0/1; the contracts pin the
  cross-boundary payload exactly (Field Registry), closing the silent-field-filtering risk.

**No new complexity-tracking entries** are introduced by the Phase 1 design.

## Next Steps (outside /speckit.plan)

1. `/speckit.tasks` — generate `tasks.md` organized by user story (US1 → US2 → US3), MVP =
   US1 (Layer 1, independently shippable).
2. `/speckit.analyze` — **MUST run before** `/speckit.implement`; the G-check will validate
   the `contracts/` Field Registry against spec/plan payload-change language.
3. `/speckit.implement` — phased: ship Layer 1, then Layer 2 observe-only, then Layer 3
   opt-in.
