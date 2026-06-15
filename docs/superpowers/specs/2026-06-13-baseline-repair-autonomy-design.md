# Baseline Repair Autonomy — Design (spec 090)

**Status:** Draft for review
**Date:** 2026-06-13
**Branch:** `090-baseline-repair-autonomy` (own spec; NOT folded into 089-implementer-local-test-gate per PR-scope discipline)

## Problem

Governing intent (user, verbatim): *"The reality is that if coordinare and its performers are not smart enough to correct a failing test that might appear to be out of scope of the issue, then it isn't going to be a useful tool."*

Today coordinare self-blocks when a card's CI is red, regardless of **whose fault** the red is. Two concrete failure modes:

1. **It merges onto a broken base.** The merge gate (`src/coordinare/graph/nodes/merge_pr.py:17-145`) and its mergeability query (`src/coordinare/services/github.py:233-243`, `1474-1487`) check **HEAD-only** mergeability. If `main` has a required check failing — something unrelated landed broken — coordinare will still merge an approved, head-green PR on top of it, compounding the breakage. `_evaluate_ci_gate` reads `base_ref` only to look up branch protection (`monitor_performer.py:1141-1152`), never to compare the base's check state.
2. **It gives up on inherited breakage.** When a card's head is red because of a failure it *inherited* from the base (not a regression it introduced), coordinare has no way to tell that apart from a regression. It blocks the card and waits for a human — exactly the "not a useful tool" outcome the user is calling out.

The dominant risk in fixing #2 is **regression-masking**: if coordinare mistakes a card-introduced regression for inherited breakage and "repairs" it by accommodating the regression (or worse, by weakening the test), it has actively damaged the codebase under a green check. The whole design is shaped around making that impossible.

## Goal

Make coordinare **classify** a red check's origin and, when it is provably **inherited** (not the card's fault), **autonomously repair the underlying code/config** — landing the fix as a reviewable commit on the card's existing branch, gated so it can never pass a check by weakening a test, and never auto-merged. When it can only pass by neutering a test, it escalates to a human instead.

One sentence: *coordinare fixes out-of-scope inherited breakage itself, behind a guard that rejects any "fix" that weakens a test, and always lands through fresh human approval.*

## Scope — three layers, one spec, phased rollout

The feature ships as three independently valuable layers, ordered so each de-risks the next. With every flag at its default (all disabled), routing/verdicts/merge decisions are **identical to the pre-feature baseline** (FR-025, SC-006).

| Layer | Priority | Nature | What it adds | Autonomy |
|-------|----------|--------|--------------|----------|
| L1 — Merge-precondition prevention gate | **P1** | New base-rollup read at the merge decision point | Refuses to merge onto a red **required** base check | None |
| L2 — Failure-origin classification | **P1** | Merge-base baseline comparison via a stable failure signature | Labels each head failure INHERITED / INTRODUCED / FLAKE / UNKNOWN, observe-only | None (observe-only) |
| L3 — Guarded autonomous repair | **P2** | Bounded repair dispatch + dual test-integrity guard | Fixes INHERITED breakage on the active branch, behind the guard | Opt-in, bounded, guarded |

L1 is load-bearing and shippable with zero autonomy. L2 is observe-only until its accuracy is trusted against real cards. L3 is opt-in per persona scope, default off.

## Layer 1 — Merge-precondition prevention gate (P1)

**Insertion point.** `src/coordinare/graph/nodes/monitor_pr.py`, between line 226 and 227 — after the PR is found `approved=True` and before `state["phase"]="merging"` is set (lines 221-228). Routing then proceeds as today: `route_from_review()` (`routing.py:25-33`) sends `phase=="merging"` → `"merge"` → `merge_pr`. The gate is a precondition *before* we commit to merging; `merge_pr.py` is left as the sole merge executor.

**New base-rollup read.** `pr_checks_service.get_pr_check_rollup(pr_number)` (`pr_checks_service.py:296-344`) is HEAD-only (`head_sha` line 197, `base_ref = pr.get("baseRefName")` line 214). We add a sibling method to fetch the **base branch / merge-base** check rollup for `base_ref`. The performer-side `get_check_runs(owner, repo, ref, token)` (`agent/performer/src/performer/github.py:106-116`) already accepts an arbitrary ref and is the reuse template for the REST shape; coordinare's version uses the GraphQL `statusCheckRollup` path it already uses for the head.

**Decision.** Reuse the spec-064 `pr_checks_policy.decide(...)` machinery (`pr_checks_policy.py:59-170`, a pure function returning `GateDecision.action ∈ {FORWARD, BOUNCE, HOLD}`) and the required-set resolution in `required_checks_resolver.resolve(...)` (`required_checks_resolver.py:30-84`) against the **base** rollup:

- Base required check in a failing conclusion → **HOLD** with a structured `base-not-green` reason naming the offending check(s) by name + URL (distinct from any head-side `FailedCheck`).
- Base required checks all green → proceed to merge as today.
- Base **non-required** failures → do **not** block (mirror head-side policy, FR-004).
- Base rollup fetch errors / times out / merge-base ambiguous → **fail-safe**: log the degraded read and fall through to today's head-only merge behavior (FR-005). Never latch — re-read each cycle (FR-006).

No new persisted state for L1; the hold is a transient decision that re-evaluates on the next monitor cycle.

## Layer 2 — Failure-origin classification (P2 work, P1 priority)

**Failure signature.** For each failing head check, compute `(name, conclusion, normalized_reason)` and compare to the base/merge-base baseline's failing checks. Today `agent/performer/src/performer/main.py:1662-1674` builds `_failure_signature` as `f"{name}|{conclusion}|{title}"` joined/sorted — **no normalization or truncation**. The new coordinare-side normalization is where the anti-masking property lives:

- `normalized_reason` is derived deterministically from stable check metadata (summary/title). Benign drift — timestamps, line numbers, run IDs, durations — is stripped so the same root cause yields the same reason across runs/environments; a genuinely different cause yields a different reason.
- **Concrete proposal (plan to finalize):** normalize the title (lowercase, collapse whitespace, strip a fixed set of volatile tokens via regex), then `sha256` the normalized string and keep a short prefix as the signature component. The exact regex set + hash/truncation length are a plan/`data-model.md` concern; the spec only fixes the *stability* and *reason-sensitivity* properties (FR-007).

**Classification rules** (`ci_gate.py` decision, extended):

- **INHERITED** — only a *stable failure* (definitive `failure` conclusion) whose full signature matches a stable failure with the same signature on the baseline (FR-008).
- **INTRODUCED** — no matching baseline signature, *including a baseline check now failing for a different normalized reason* (FR-009; the anti-masking rule, mandatory).
- **FLAKE** — transient conclusions (`timed_out`, `cancelled`, `neutral`, `skipped`) on either side never produce INHERITED; routed through existing flake handling (FR-010, FR-011). A flaky/non-deterministic baseline check can never anchor an INHERITED label — the corresponding head failure is conservatively INTRODUCED or UNKNOWN.
- **UNKNOWN** — baseline indeterminate; treated as the card's own, no INHERITED labels, downstream unchanged (FR-012).

Note the conclusion partition already exists performer-side: `_FAILING_CONCLUSIONS = {failure, timed_out, cancelled, action_required}`, `_PASSING_CONCLUSIONS = {success, neutral, skipped}` (`performer/github.py:159-184`). Only the definitive `failure` conclusion forms a *stable* signature for INHERITED; the rest are transient/non-stable for classification purposes.

**Data model.** Extend `CIGateDecision` (`ci_gate.py:59-70`) with `inherited_checks` / `introduced_checks` lists (insert ~line 65), each carrying the `FailedCheck` (`ci_gate.py:43-56`: `name, conclusion, html_url, last_log_line`) plus the signature that justified the label. `Verdict` (`ci_gate.py:17`: `pass|hold|bounce|escalate`) is unchanged in L2.

**Observe-only invariant (FR-014, SC-006).** In L2, classification adds evidence to the persisted decision and observability **only** — it must not alter routing or verdict. Validate accuracy against real cards before L3 acts on it.

## Layer 3 — Guarded autonomous repair (P3 work, P2 priority)

**Eligibility.** Only INHERITED stable failures, only when repair is enabled for the persona scope (default off, FR-015/FR-016). INTRODUCED / FLAKE / UNKNOWN are never auto-repaired (introduced regressions stay the card's own responsibility via existing flows).

**Dispatch.** Reuse the spec-089 coordinare self-fix loop as the template (`monitor_performer.py:2335-2396`): it increments `state["local_fix_counter"]`, sets `relay_feedback` / `performer_stage` / `phase="dispatching"`, and on exhaustion (2372-2396) sets `phase="blocked"`. The baseline-repair dispatch mirrors this against the card's **existing active branch** (FR-017) with a repair-mandate instruction that requires a code/config fix and forbids passing the check by weakening, skipping, deleting, mocking-away, or loosening any test (FR-018). The implementer persona's repair-mandate scope instruction attaches in `persona_service.py:196-269` (alongside the existing "## Run the tests before you finish (089)" block at 220-229).

**Dual test-integrity guard (FR-019).** Every candidate repair diff must pass *both*:

1. **Static heuristic** (new pure service, e.g. `services/test_integrity_guard.py`) — flags, at minimum: deleted assertions; loosened assertions / comparison operators; assertions made conditional or wrapped in swallowing `try/except`; added `skip`/`xfail`/`disable` markers; removed setup/teardown; tests mocked away from the asserted requirement. Pure and fixture-testable.
2. **Automated adversarial reviewer** — an independent judging pass in a **separate context** from the agent that wrote the repair, judging whether the change weakens coverage. This is **not** the human approval (Clarification Q3); it is an automated component of the guard.

Both are **conservative**: a change the guard cannot confidently clear is treated as a weakening. **Either** component vetoing (or being unsure) rejects the repair and escalates (FR-020) — no push, no merge.

**Landing & merge boundary (FR-021).** The guard governs whether a repair may *land as a candidate*, never whether it may *merge*. An accepted repair commit lands on the active branch, **invalidates any prior PR approval**, and requires a **fresh human approval** before merge. Coordinare never auto-merges on the guard's verdict.

**Budget (FR-022).** A per-head attempt counter, parallel to spec-075's `bounce_counter` (`state_store.py:110`) and spec-089's `local_fix_counter` (`state_store.py:114`). New field `inheritance_repair_counter: dict[str, int]` inserts after line 114; `CURRENT_SCHEMA_VERSION` (line 18) bumps **8 → 9**; old snapshots load `{}` (FR-026). A new head commit starts a fresh budget; exhaustion escalates rather than loops.

**Escalation visibility (FR-024).** Every escalation path (guard rejection, budget exhaustion, indeterminate-but-required human judgment) produces a visible, actionable signal — surfaced on the card/PR and reflected in the card's phase (a human-review/`blocked` state) — never log-only.

## Configuration

New `InheritedRepairGateConfig` parallel to `CIGateConfig` (`config.py:1319-1327`) and `LocalTestGateConfig` (`config.py:1329-1342`), nested under `PersonaScopeConfig` (`config.py:1350-1371`):

- `enabled: bool = False` (opt-in, FR-015)
- `max_repair_attempts_per_head` (ge=0 le=20, default small — mirrors `max_fix_attempts` / `max_bounces_per_head` shape; `0` = classify/prevent only, never repair)
- any guard thresholds

L1 and L2 gating: L1 (prevention) and L2 (classification) are themselves flag-gated so the full feature can be rolled out L1 → L2-observe → L3-opt-in. With all flags default, behavior is the pre-feature baseline (FR-025, SC-006).

## Error handling & edge cases — FAIL-SAFE

- **Indeterminate baseline** (fetch error/timeout/ambiguous merge-base): no INHERITED labels, no repair, existing gate behavior preserved (FR-005, FR-012). Never blocks indefinitely.
- **Systemic baseline-fetch failure** (e.g. rate-limit exhaustion): a degraded baseline silently suppresses inherited classification *and* repair — so repeated/systemic failure must raise an **operator-visible** signal distinct from a per-evaluation debug log (FR-027), so degradation can't be weaponized unnoticed.
- **Regression resembling inherited breakage**: same check fails on both sides but for a different normalized reason → INTRODUCED, never auto-repaired (FR-009). Core anti-masking guarantee.
- **Guard cannot decide / false-positive on a legitimate refactor**: escalate to a human, never silently accept — erring toward human review is the intended bias.
- **Base advances mid-flight**: gate re-reads each evaluation, not latched (FR-006).
- **Backward compatibility**: pre-feature snapshots load with new fields at safe empty defaults; empty repair counter means "no attempts yet" (budget still applies), never "unlimited" (FR-026).

## Testing (TDD — RED first)

- **L1** — `tests/unit/graph/nodes/test_monitor_pr.py`: approved + head-green + base required check `failure` → no merge, `base-not-green` hold naming the base check; base flips green → merge proceeds; base-rollup fetch errors → fall back to head-only merge (fail-safe); base non-required failure → not blocked.
- **L2** — `tests/unit/services/test_ci_gate*.py` (+ a classification unit): stable failure matching baseline → INHERITED; previously-green base check now failing → INTRODUCED; same check, different normalized reason → INTRODUCED (anti-masking, zero tolerance); baseline transient (timed-out/cancelled) → NOT INHERITED; baseline indeterminate → all UNKNOWN, downstream unchanged; observe-only assertion that routing/verdict are byte-identical to today with labels present.
- **L3** — `test_test_integrity_guard.py`: adversarial corpus — (a) removed assertion, (b) `xfail`/`skip`, (c) loosened comparison, (d) swallowing `try/except`, (e) mocked-away requirement → each rejected (static heuristic flags a–c, e); clean code-only fix → passes both guards. `test_monitor_performer.py`: INHERITED + enabled → one dispatch on existing branch with repair-mandate instruction; accepted repair → commit lands, prior approval invalidated, awaits fresh human approval, NOT auto-merged; budget exhaustion → visible escalation, no further dispatch; INTRODUCED/FLAKE/UNKNOWN → no dispatch; disabled scope → exactly L1+L2.
- **Migration** — `tests/contract/test_state_persistence_v8_to_v9.py` (analog of the existing `test_state_persistence_v7.py` pattern): v8 snapshot loads under v9 with `inheritance_repair_counter` defaulted to `{}`, behavior preserved. Update the `schema_version == 8` assertion at `tests/contract/test_state_persistence.py:131` to 9.

## Constitution alignment

`.specify/memory/constitution.md` Principle II (Testing Discipline, NON-NEGOTIABLE) is the direct authority for the test-integrity guard: an autonomous repair that weakens a test would violate it, which is exactly what the dual guard exists to make impossible.

## Decomposition & order

L1 → L2 (observe-only) → L3 (opt-in), within one spec. L1 is independently shippable and the highest-severity/lowest-complexity slice. L2 must be validated for accuracy in production before L3 acts on it. L3 is gated, bounded, and default-off.

## Non-goals

- No change to the merge executor itself (`merge_pr.py` stays the sole merge step; L1 is a precondition before it).
- No auto-merge of any repair — fresh human approval is always required (FR-021).
- No repair of INTRODUCED regressions — those remain the card's own responsibility via existing flows.
- No new persisted state for L1 (transient hold); the only new persisted field is the per-head repair counter (schema v9).
- No relaxation of existing flake handling — it is reused, not replaced.
