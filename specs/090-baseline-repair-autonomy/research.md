# Phase 0 Research: Baseline Repair Autonomy

**Branch**: `090-baseline-repair-autonomy` | **Date**: 2026-06-13 | **Plan**: [plan.md](plan.md)

This document resolves every implementation-level NEEDS CLARIFICATION the plan raised, so
no clarification is deferred to implement time (Constitution V). Each decision carries a
**measurable performance budget** (Constitution IV — "measured, not assumed") and names the
benchmark or test that proves it. All adversarial corrections from the grounding pass are
folded in; where a naive reading of the spec or design doc would be wrong against the
current code, the correction is stated explicitly under **Correction**.

Grounding anchors (verified against HEAD this cycle):

- `ci_gate.py:17` `Verdict`; `:23-40` `compute_ci_gate_signature` (**16-char** SHA-256
  precedent: `hexdigest()[:16]`); `:43-56` `FailedCheck(BaseModel, extra="forbid")`;
  `:59-100` `CIGateDecision(BaseModel, extra="forbid")`.
- `pr_checks_service.py:34-44` `CheckEntry(frozen)` (no title/summary); `:46-57`
  `CheckRollup(frozen)`; `:62-97` `_ROLLUP_CORE` (CheckRun fragment fetches
  `name,status,conclusion,detailsUrl` — **no `output`**); `:99-115` `_ROLLUP_QUERY`
  (with BPR); `:120-128` `_ROLLUP_QUERY_NO_BPR`; `:185-277` `parse_rollup`; `:296-344`
  `get_pr_check_rollup` (HEAD-only — **no base-rollup method exists**).
- `pr_checks_policy.py:32-42` pass/fail conclusion sets; `:59-170` `decide()` (pure; when
  `branch_protection_readable=False` → `required=[]`/ADVISORY ~`:125-135`; the 075 path
  `:82-116` forces blocking only when `required_check_names` is passed).
- `required_checks_resolver.py:30-84` `resolve(*, scope, branch_protection_set, all_head_checks, persona_check_map)`.
- `state_store.py:18` `CURRENT_SCHEMA_VERSION: int = 8`; `PersistedSession.bounce_counter`
  (v5, `:110`), `local_fix_counter` (v8, `:114`), `ci_gate_rollup_signature` (v6, `:117`).
- `config.py:1319-1327` `CIGateConfig`; `:1329-1342` `LocalTestGateConfig`; `:1350-1371`
  `PersonaScopeConfig` (`ci_gate`/`local_test_gate` nested).
- `monitor_pr.py` L1 hook: between `approved=True` (~`:221`) and `if approved:` /
  `state["phase"]="merging"` (~`:227-228`).
- `monitor_performer.py` `_evaluate_ci_gate` `:1068-1379` (base_ref read `:1141-1152`;
  decision construction `:1195-1360`; classification insertion point ~`:1172`); spec-089
  self-fix loop `:2335-2396` (counter `:2356`, exhaustion `phase="blocked"` `:2383`).
- `persona_service.py:196-269` `DEFAULT_INSTRUCTIONS['implementer']`; "## Run the tests
  before you finish (089)" `:220-229`.
- Performer (unchanged this feature): `agent/performer/src/performer/github.py:91-92`
  `_FAILING_CONCLUSIONS={failure,timed_out,cancelled,action_required}`,
  `_PASSING_CONCLUSIONS={success,neutral,skipped}`.

---

## Decision 1 — Failure-signature normalization algorithm (FR-007, FR-009)

**Decision.** Add a new pure module `src/coordinare/services/failure_signature.py` exposing
two functions:

```python
def normalize_reason(title: str | None, summary: str | None) -> str: ...
def make_failure_signature(
    name: str, conclusion: str, title: str | None, summary: str | None
) -> tuple[str, str]:  # (signature_hash, normalized_reason)
```

`normalize_reason` selects the **title** as the primary reason text, falling back to
**summary** when title is empty/absent, and the empty string `""` when both are absent. It
lowercases, collapses whitespace, and strips benign wording drift via a set of regexes
**compiled once at module load**:

| Drift class | Regex (illustrative) | Replaced with |
|-------------|----------------------|---------------|
| ISO timestamps | `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?` | `<ts>` |
| Durations | `\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|m|min|h)\b` | `<dur>` |
| Run / job IDs | `\b(?:run|job|build)[ _-]?\d{3,}\b` | `<id>` |
| Long hex / SHAs | `\b[0-9a-f]{16,}\b` | `<hex>` |
| UUIDs | `\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b` | `<uuid>` |
| Paths-with-hashes | `/[^\s]*[0-9a-f]{8,}[^\s]*` | `<path>` |
| Line/col numbers | `:\d+(?::\d+)?\b` | `:<n>` |

The signature hash is `hashlib.sha256(f"{name}\x1f{conclusion}\x1f{normalized_reason}".encode()).hexdigest()[:16]`
— **16 chars**, matching the `compute_ci_gate_signature` precedent and satisfying FR-009's
reason-sensitivity (a 12-char hash was rejected — see Alternatives).

**Correction (anti-masking depends on text the current query does not fetch).** The
coordinare GraphQL CheckRun fragment (`_ROLLUP_CORE`, `pr_checks_service.py:80-85`) fetches
only `name,status,conclusion,detailsUrl`. There is **no failure text** in the rollup today,
so `normalize_reason` would receive `(None, None)` for every check and every signature
would collapse to `name|conclusion|""` — which is exactly the name+conclusion-only signature
FR-009 forbids. Therefore this decision **requires**, as a hard prerequisite:

1. Extend the CheckRun fragment with `output { title summary }` (text is the largest
   field; we fetch title+summary, not `text`, to bound payload — see budget).
2. Extend `CheckEntry` (frozen) with `title: str | None = None`, `summary: str | None = None`.
3. Update `parse_rollup()` (`:185-277`) to populate them and update all rollup fixtures.

**Runtime collision detection.** Because we truncate to 16 chars, `ci_gate.compare_signatures()`
(new helper) MUST detect the case where two checks with **distinct normalized reasons** hash
to the **same 16-char value** and, if it occurs, treat the comparison as indeterminate and
escalate (UNKNOWN), never as a match. This closes the only path by which truncation could
mask a regression (Principle II — deterministic, non-masking).

**Performance budget.** `normalize_reason` + hash MUST run in **≤ 50 µs per check** (regexes
precompiled; ≤ 100 checks per rollup ⇒ ≤ 5 ms total, well inside the Layer 2 ≤ 50 ms p95
budget). Added GraphQL payload from `output{title summary}` MUST stay **≤ 8 KB per rollup
p95** (title+summary only, not `text`). Proven by `test_failure_signature.py`
micro-benchmark + a payload-size assertion in `test_pr_checks_service.py`.

**Rationale.** Title-primary/summary-fallback matches how GitHub Checks surface a failure's
one-line cause; the drift regexes target precisely the volatile substrings FR-007 enumerates
(timestamps, line numbers, run IDs) plus the obvious extras (UUIDs, hashes, durations).
Compiling at module load keeps the hot path allocation-free. 16 chars matches the existing
in-repo hash precedent, so reviewers see one hashing convention, not two.

**Alternatives considered.**
- *Name+conclusion only (no reason)* — rejected: cannot satisfy FR-009 anti-masking (same
  check failing for a different cause would falsely match INHERITED).
- *12-char hash* — rejected: violates FR-009's reason-sensitivity margin and diverges from
  the established 16-char `compute_ci_gate_signature` precedent for no payload benefit.
- *Hash the raw `text` body* — rejected: unbounded payload, and full bodies carry the most
  drift (full stack traces), defeating stability; title/summary is the stable reason carrier.
- *NLP/semantic similarity* — rejected: non-deterministic, violates Principle II
  (deterministic tests) and FR-007 (same input → same signature).

**Mandate.** An **anti-masking corpus** (SC-003) is required: paired (baseline, head) check
fixtures where the same `(name, conclusion)` fails for the *same* reason (→ INHERITED) and
for a *different* reason (→ INTRODUCED), plus drift-only variants that MUST hash identically.

---

## Decision 2 — Base-branch rollup fetch (FR-001, FR-002, FR-005, FR-027)

**Decision.** Add a **new** GraphQL query `_BASE_ROLLUP_QUERY` and a new method
`get_base_branch_check_rollup(base_ref: str) -> CheckRollup | None` on `pr_checks_service`,
plus a `parse_base_rollup()` parser. The query targets the branch ref's tip commit:

```graphql
reference(qualifiedName: $ref) {
  target {
    ... on Commit {
      oid
      pushedDate
      committedDate
      statusCheckRollup { ... }   # same rollup sub-selection as head, incl. output{title summary}
    }
  }
}
```

**Correction (cannot reuse `_ROLLUP_CORE`).** The PR-rollup query hard-codes PR-only fields
(`number`, `baseRefName`, the `commits(last:1)` navigation). A `Ref`/`Commit` target has
none of these — reusing `_ROLLUP_CORE` against `reference(qualifiedName:)` is a GraphQL
**type error**, not a smaller change. A separate query + parser is the honest design
(Principle I — prefer duplication over interface bloat).

**Correction (FR-002 requires the 075 path, not the default 064 path).** The default
`decide()` behaviour when `branch_protection_readable=False` is `required=[]`/ADVISORY
(`pr_checks_policy.py:125-135`). If we fetched the base rollup *without* its branch-protection
rules, base required failures would be classified ADVISORY and **would not block** — silently
violating FR-002. Therefore `get_base_branch_check_rollup` MUST also fetch the base branch's
`branchProtectionRules`, resolve the base required set via `required_checks_resolver.resolve(...)`,
and call `pr_checks_policy.decide(base_rollup, required_check_names=base_required_set)` (the
spec-075 path, `:82-116`) so base required failures BLOCK and non-required ones do not (FR-004).

**Fail-safe (FR-005, SC-002).** The method returns `None` on any error, timeout, or
indeterminate target (ref missing, target not a Commit, no rollup). The L1 caller treats
`None` as "base indeterminate → today's head-only behaviour" — never an indefinite block.

**Systemic-failure signal (FR-027).** Maintain a per-repo `collections.deque` of fetch-failure
timestamps as an **instance variable on the service** (NOT a `CheckRollup` field — the DTO is
frozen and read-only). On each failure: append, prune entries older than 1 hour, and if the
window holds **≥ 5** failures, emit a structlog **error** event `baseline_fetch_degraded`
(repo, window-count) — distinct from the per-evaluation debug log. This is the operator-visible
signal FR-027 mandates so a weaponized degradation (e.g. rate-limit exhaustion) cannot pass
unnoticed. Timestamps are sourced from the caller (the script runtime forbids `Date.now()` in
workflow scope, but coordinare production code uses normal `time`/`datetime` — the deque uses
`time.monotonic()`).

**DTO shape.** `CheckRollup` is frozen. To distinguish a base rollup from a head rollup
downstream, add an optional `rollup_origin: Literal["head", "base"] = "head"` field (additive,
defaulted — preserves all existing construction sites) rather than a parallel DTO class.

**Performance budget.** Head and base rollups MUST be fetched **concurrently** via
`asyncio.gather`, so Layer 1 adds **≤ 500 ms p95** to a merge-decision cycle (one extra
round trip overlapped with the head fetch, not serialized). Proven by an
`asyncio`-instrumented timing assertion in `test_pr_checks_service.py` using a stubbed
transport with injected latency. At flag default (L1 disabled) **zero** added round trips.

**Rationale.** A dedicated query keeps the head path untouched and lets the base path fetch
exactly what FR-002 needs (its own BPR + rollup). Returning `None`-on-error makes fail-safe
the structurally-default behaviour. The deque is the minimum stateful mechanism that can
distinguish a one-off blip from systemic degradation without persisting anything.

**Alternatives considered.**
- *Reuse `get_pr_check_rollup` with a synthetic PR* — impossible; there is no PR for a bare base.
- *Compare against the literal merge-base commit's checks* — rejected: a merge-base commit
  often has no check run at all; the base *branch tip* is what "is main red right now?"
  actually asks, matching US1's intent ("base branch ... currently has a required check
  failing").
- *Persist the base rollup* — rejected: FR-006 requires re-read each cycle (no latch); the
  base state is read-only evidence, never authoritative state.

---

## Decision 3 — Classification model & decision table (FR-008..FR-012, FR-013)

**Decision.** Extend `CIGateDecision` with four additive classification lists and add a pure
classifier. New model `FailedCheckWithSignature(FailedCheck)` adds
`head_signature: str` and `baseline_signature: str | None = None`. On `CIGateDecision`:

```python
inherited_checks:  list[FailedCheckWithSignature] = Field(default_factory=list)
introduced_checks: list[FailedCheckWithSignature] = Field(default_factory=list)
flake_checks:      list[FailedCheck]              = Field(default_factory=list)
unknown_checks:    list[FailedCheck]              = Field(default_factory=list)
```

New pure helpers in `failure_classification.py`:
`_is_stable_failure(conclusion)`, `_is_transient_conclusion(conclusion)`,
`classify_failure_origin(head_check, baseline_index) -> Classification`.

**Decision table — evaluated in SOURCE ORDER, first match wins** (per FR-008..FR-012):

1. Head conclusion is **transient** → **FLAKE**. (FR-010)
2. Baseline indeterminate (no baseline rollup / collision detected) → **UNKNOWN**. (FR-012)
3. A baseline check with the **same name** exists but its conclusion is **transient** (flaky
   baseline) → **INTRODUCED**. (FR-011 — a flaky baseline never anchors INHERITED)
4. A baseline **stable** failure with a **matching signature** exists → **INHERITED**. (FR-008)
5. Otherwise (no baseline match, or same name + stable but **different** signature) →
   **INTRODUCED**. (FR-009 anti-masking)

**Observe-only invariant (Layer 2, FR-014).** Add a `model_validator` on `CIGateDecision`
asserting **exactly-one-classification**: every name in `failed_checks` appears in exactly
one of the four lists, and no classification list contains a name absent from `failed_checks`.
This is an evidence-integrity check; it does NOT touch `verdict` (observe-only preserved).

**Persistence/observability (FR-013).** The four lists serialize on the persisted
`CIGateDecision`; classification counts + per-check `(label, head_signature, baseline_signature)`
are emitted as a structlog event at the existing decision-construction site
(`monitor_performer.py:1195-1360`). Insertion of the classify call is immediately **before**
`decide()` at ~`:1172`.

**Performance budget.** Classification over ≤ 100 head checks against a ≤ 100-entry baseline
index (a dict keyed by `(name, signature)`) MUST add **≤ 50 ms p95** to a gate evaluation —
pure in-process, no I/O (the base rollup is already fetched by Decision 2). Proven by a
micro-benchmark in `test_failure_classification.py`.

**Rationale.** Source-order/first-match-wins makes the table auditable and makes FLAKE and
UNKNOWN strictly dominate INHERITED, which is the conservative bias the spec demands (a
transient or indeterminate signal can never produce an auto-repairable INHERITED label).
Rule 3 (flaky-baseline → INTRODUCED) directly encodes FR-011. Additive `default_factory=list`
fields keep pre-feature snapshots loading unchanged (FR-026).

**Alternatives considered.**
- *Single enum field per check instead of four lists* — rejected: the four-list shape mirrors
  the existing `failed_checks`/`pending_checks` partition on `CIGateDecision`, and lists carry
  the per-check signatures needed for audit (FR-013).
- *Classify inside `decide()`* — rejected: `decide()` is a pure policy function shared with
  the head/base L1 path; classification is L2-specific and must stay observe-only. Keeping it
  in a separate helper preserves `decide()`'s single responsibility.

---

## Decision 4 — Layer 3 repair dispatch (FR-015..FR-018, FR-022, FR-023)

**Decision.** Reuse the spec-089 bounded self-fix loop in `monitor_performer`
(`:2335-2396`) as the structural template. Eligibility gate:

```
inherited_repair_gate.enabled
  and inherited_repair_gate.max_repair_attempts_per_head > 0
  and len(decision.inherited_checks) > 0
```

A new per-head counter `inheritance_repair_counter[head_sha]` (Decision 6) is read with
`.get(head_sha, 0)` (a new head starts at 0) and **incremented at dispatch time**. The
dispatch reuses the existing `JobInitPayload` channel; the repair instruction travels as a
new `metadata["repair_mandate"]` object (contract in
[contracts/repair-dispatch.md](contracts/repair-dispatch.md)):

```json
{
  "type": "baseline_repair",
  "inherited_checks": [
    {"name": "...", "conclusion": "failure", "normalized_reason": "...", "html_url": "..."}
  ],
  "attempt": 1,
  "max_attempts": 1,
  "instruction": "Fix the underlying code/config so these inherited checks pass. You MUST NOT make a check pass by weakening, skipping, deleting, mocking away, or loosening any test."
}
```

The implementer persona gains a scope instruction attached after
`persona_service.py:229` (the existing "## Run the tests before you finish (089)" block)
that explains baseline-repair mode and the test-weakening prohibition. **No performer code
change** — the performer already reads `JobInitPayload.metadata`.

**Exhaustion (FR-022, FR-024, SC-007).** On budget exhaustion, mirror the 089 path: set
`phase="blocked"`, append an `open_questions` entry, and post a GitHub comment — a visible,
actionable escalation, never log-only.

**Performance budget.** The repair dispatch is an **out-of-band performer turn** (30–90 s),
explicitly **off** the merge-decision hot path; it runs only when L3 is enabled AND an
INHERITED failure exists. It adds **zero** latency to the L1/L2 evaluation path and **zero**
cost at flag default. Proven by `test_monitor_performer_ci_gate.py` asserting no dispatch
occurs when the gate is disabled or no INHERITED check exists.

**Rationale.** The 089 loop already solves counter-keyed bounded dispatch with escalation;
reusing it avoids a second divergent self-fix mechanism. Putting the mandate in
`metadata.repair_mandate` rather than overloading an existing field keeps the contract
explicit and registrable (closing the silent-field-filtering risk the G-check guards).

**Alternatives considered.**
- *New branch/PR for the repair* — rejected: FR-017 mandates the card's existing active branch.
- *Increment the counter on acceptance instead of dispatch* — rejected: a guard-rejected or
  crashed attempt must still consume budget, else a pathological loop never exhausts (SC-007).
- *Reuse `local_fix_counter`* — rejected: 089's local-fix budget is a different concern
  (pre-handoff local tests); conflating them would let one drain the other (FR-022 wants a
  parallel, independent counter).

---

## Decision 5 — Test-integrity guard (FR-019, FR-020, FR-021)

**Decision.** A **dual** guard. (a) A static module
`src/coordinare/services/test_integrity_guard.py`:

```python
def analyze_diff(diff: str) -> tuple[bool, list[str]]:  # (is_safe, flagged_patterns)
```

It scans the unified diff for, at minimum: deleted assertions, loosened comparison operators
(`==`→`>=`/`<=`/`!=` weakenings, tolerance widening), assertions made conditional or wrapped
in swallowing `try/except`, added `@pytest.mark.skip`/`xfail`/`disable`, removed
`setUp`/`tearDown`/fixtures, and requirements mocked away (a previously-real call replaced by
a mock that returns the asserted value). It is **conservative**: a diff touching test files in
a way it cannot confidently clear returns `is_safe=False` (FR-019 — "cannot confidently clear
⇒ weakening").

(b) An **automated adversarial reviewer**: a separate **`diagnostic`-role performer dispatch**
(30–90 s) given the diff and asked, in a fresh context, to judge whether it weakens test
coverage.

**Correction (a diagnostic judge does NOT violate FR-017).** FR-017 governs where the
repair *implementation* lands (the card's existing branch). The guard *evaluation* is a
read-only judgment that writes nothing to the branch, so dispatching it as a separate
`diagnostic` performer is permitted and is exactly the "separate context from the agent that
produced the repair" FR-019 demands.

**Correction (drop the `guard_lm_model` knob).** The design doc's speculative
`guard_lm_model` config is rejected (Principle I — no dead code). The adversarial reviewer is
a `diagnostic` dispatch through the existing routing, not an in-process LLM client, so no new
model-client dependency and no new config knob are introduced.

**Verdict (FR-020, FR-021).** Either component vetoing — or being unable to confidently clear —
rejects the repair, escalates to a human (visible signal, `phase="blocked"`), and **never
pushes**. The dual guard governs only whether a repair may *land as a candidate*; it never
governs *merge*. An accepted repair commit invalidates prior approval and requires fresh human
approval (Decision 7).

**Performance budget.** Static `analyze_diff` MUST run in **≤ 100 ms p95** for a diff
≤ 2 000 changed lines (pure regex/line scan; proven by `test_test_integrity_guard.py`). The
adversarial reviewer dispatch is off the hot path (30–90 s, out-of-band) and runs only on an
accepted-by-static candidate.

**Rationale.** Two independent failure modes (mechanical weakening the regex catches; subtle
semantic weakening a model catches) need two independent detectors; either-vetoes maximizes
recall of weakenings (SC-004 wants 100% rejection of the corpus). Conservative-on-uncertainty
biases toward human review, the spec's stated intended bias.

**Alternatives considered.**
- *Static-only guard* — rejected: misses semantic weakenings (mock-away, logically-dead
  assertions) the spec's corpus includes.
- *Reviewer-only guard* — rejected: non-deterministic, can't guarantee the SC-004 mechanical
  cases; the static heuristic gives a deterministic floor.
- *In-process LLM judge with `guard_lm_model`* — rejected (above): dead config + new
  dependency + no real context isolation.

**Mandate.** An **adversarial test-weakening corpus** (SC-004): diffs that (a) remove an
assertion, (b) add `xfail`/`skip`, (c) loosen a comparison, (d) wrap an assertion in
swallowing `try/except`, (e) mock away the asserted requirement — each MUST be rejected, with
the static heuristic alone flagging (a)–(c) and (e).

---

## Decision 6 — State & config migration (FR-025, FR-026, SC-009)

**Decision (state).** Bump `state_store.py` `CURRENT_SCHEMA_VERSION` **8 → 9**. Add to
`PersistedSession`, immediately after `local_fix_counter` (`:114`):

```python
inheritance_repair_counter: dict[str, int] = Field(default_factory=dict)  # v9 (spec-090)
```

Extend the migration comment block (`:20-38`) with a v9 line. An **empty** counter means "no
attempts recorded yet — the configured budget still applies"; it never means "unlimited"
(FR-026). `MIN_SUPPORTED_SCHEMA_VERSION` stays 1; v1–v8 snapshots load with the new field
defaulted (SC-009).

**Decision (config — Option B, pattern-consistent).** Do **NOT** nest L1/L2 flags inside the
repair-gate class. Add **three** classes (`extra="forbid"`, mirroring `CIGateConfig` /
`LocalTestGateConfig`), inserted ~`config.py:1344` and nested on `PersonaScopeConfig`
after `:1371`:

```python
class BaselinePreventionGateConfig(BaseModel):      # Layer 1
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False

class BaselineClassificationGateConfig(BaseModel):  # Layer 2 (observe-only)
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False

class InheritedRepairGateConfig(BaseModel):         # Layer 3
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_repair_attempts_per_head: int = Field(default=1, ge=0, le=20)
```

On `PersonaScopeConfig`:
`baseline_prevention_gate = Field(default_factory=BaselinePreventionGateConfig)`,
`baseline_classification_gate = Field(default_factory=BaselineClassificationGateConfig)`,
`inherited_repair_gate = Field(default_factory=InheritedRepairGateConfig)`.

**All three default `enabled=False`** ⇒ at defaults, every added path is inert and decisions
are byte-identical to baseline (FR-025, SC-006).

**Correction (do not couple the layers).** The design doc's single-class-with-nested-flags
shape was rejected: it couples three independently-shippable layers and breaks the
phased-rollout flag isolation. Three classes match the established one-class-per-layer pattern
and let L1 ship while L2/L3 stay off.

**Performance budget.** Migration is a one-time load-path cost: loading a v8 snapshot and
defaulting the new field MUST add **≤ 1 ms** over a v8 load (a single dict default). Proven by
`test_state_persistence_v8_to_v9.py`.

**Rationale.** A new counter parallel to `bounce_counter`/`local_fix_counter` is the
established budgeting shape. Three small config classes keep each layer's flag independent and
each class matches the existing gate-config idiom exactly.

**Alternatives considered.**
- *Reuse `local_fix_counter` for repair attempts* — rejected (see Decision 4).
- *Single `InheritedRepairGateConfig` with `prevention_enabled`/`classification_enabled` flags*
  — rejected: couples layers, breaks SC-006 per-layer isolation.
- *Schema-versionless additive field* — rejected: the repo's contract test asserts the exact
  version (`test_state_persistence.py:131`); a bump + migration test is the established
  discipline.

**Mandate.** Update `test_state_persistence.py:131` (`==8` → `==9`); create
`test_state_persistence_v8_to_v9.py` mirroring the v7 template.

---

## Decision 7 — Approval invalidation on repair commit (FR-021, SC-005)

**Decision.** Rely on **GitHub-native approval dismissal on new commits** (branch protection's
"Dismiss stale pull request approvals when new commits are pushed"). An accepted repair commit
pushed to the active branch dismisses any prior approval; the PR then needs a fresh human
approval before coordinare's existing closer-boundary merge gate will advance it. Coordinare
adds nothing to *force* a merge — its merge path already requires `approved=True`
(`monitor_pr.py:221`), so a dismissed approval automatically blocks the merge until re-approval.

**Documented assumption.** This requires the repo's branch protection to enable
"dismiss stale approvals". `quickstart.md` records this as a Layer 3 precondition, and the
escalation comment posted on dispatch states it explicitly so a maintainer cannot mistake a
landed repair for a pre-approved one.

**Performance budget.** Zero added cost — no new API call; the dismissal is a GitHub-side
effect of the push coordinare already performs.

**Rationale.** GitHub already implements exactly the semantics FR-021 wants; re-implementing
approval-state tracking in coordinare would duplicate platform behaviour and risk drift. The
closer-boundary merge gate's existing `approved` precondition makes this fail-closed: if
dismissal is misconfigured, the worst case is a *human* re-reviewing, never an auto-merge.

**Alternatives considered.**
- *Coordinare-side approval invalidation* (track approval SHA, refuse merge if a commit lands
  after it) — viable but duplicates GitHub's native feature; deferred as a fallback only if a
  target repo cannot enable dismissal. Recorded, not built (Principle I — no speculative code).
- *Force-push amend to preserve approval* — rejected: explicitly violates FR-021 (the repair
  MUST invalidate prior approval, not preserve it).

---

## Decision 8 — Layer 1 gate wiring (FR-001..FR-006) *(self-authored — workflow stage failed)*

> The grounding workflow's `l1-gate-wiring` stage failed to return structured output; this
> decision is authored from the base-rollup-fetch corrections (Decision 2), the approved
> design doc, and the verified `monitor_pr.py` insertion point.

**Decision.** Insert the L1 base-not-green check in `monitor_pr.py` between the point where
`approved` is established (~`:221`) and the `if approved: state["phase"]="merging"` advance
(~`:227-228`). Pseudocode:

```python
# existing: approved = True; state["pending_reviews"] = actionable  (~:221-226)
if approved and prevention_gate_enabled(scope):
    base_decision = await evaluate_base_gate(pr)          # uses Decision 2 method + 075 decide()
    if base_decision is BLOCK:                            # base required check failing
        record_base_not_green_hold(state, base_decision)  # structured, names check+URL (FR-003)
        state["phase"] = <hold/re-eval phase>              # NOT "merging"; re-evaluated next cycle (FR-006)
        return                                             # do not advance to merge
    # base_decision is None (indeterminate) OR ALLOW → fall through to today's behaviour (FR-005)
if approved:
    state["phase"] = "merging"                            # unchanged
```

Key properties wired here:

- **FR-002**: `evaluate_base_gate` uses Decision 2's `get_base_branch_check_rollup` + base BPR
  + `pr_checks_policy.decide(..., required_check_names=base_required_set)` (075 path), so only
  *required* base failures BLOCK.
- **FR-004**: non-required base failures yield ALLOW (advisory) → merge proceeds.
- **FR-005 / SC-002**: `get_base_branch_check_rollup` returning `None` (indeterminate) →
  fall through to today's head-only merge; **never** an indefinite block.
- **FR-006**: no latch — the gate re-evaluates `base_decision` on every monitor cycle; a base
  that turns green proceeds, one that turns red holds.
- **FR-003**: the hold record names the offending base check(s) by name + URL, distinct from
  any head-side failure record (a base-not-green hold reason on the gate decision).
- **FR-025 / SC-006**: guarded by `prevention_gate_enabled(scope)`; at default (disabled) the
  block is skipped entirely — zero added round trips, identical merge behaviour.

**Performance budget.** When enabled, L1 adds the single base-rollup round trip from
Decision 2, fetched **concurrently** with the head rollup ⇒ **≤ 500 ms p95** added per merge
cycle. When disabled, **zero** added cost. Proven by `test_monitor_pr.py` (enabled: base-red
holds, base-green proceeds, indeterminate falls through; disabled: byte-identical to baseline).

**Rationale.** This is the lowest-complexity, highest-severity slice (US1): a single read at
the existing decision point, no classification, no dispatch. Placing it *after* `approved` and
*before* `phase="merging"` reuses the exact gate the closer already trusts, so it composes with
spec-064/075 without touching `merge_pr.py` or the mergeability query.

**Alternatives considered.**
- *Gate at `merge_pr.py` / `check_mergeability`* — rejected: those are HEAD-only and run later;
  US1 wants the hold at the *decision* point so the card visibly holds-and-re-evaluates rather
  than failing a mergeability probe.
- *Latch the base result* — rejected: violates FR-006 (base can turn green mid-flight).
- *Block on any base failure* — rejected: violates FR-004 (only required checks gate).

---

## Cross-cutting: stable-vs-transient conclusion enumeration (FR-008, FR-010, FR-011)

The classifier (Decision 3) and the performer share a conclusion vocabulary, but **do not use
the same partition**, and the plan must state both to avoid a silent mismatch:

- **Stable failure (INHERITED-eligible)** — exactly `conclusion == "failure"`. Only a
  definitive `"failure"` forms a stable signature (FR-008, Key Entities "Failure signature").
- **Transient (never INHERITED; → FLAKE/INTRODUCED/UNKNOWN)** — the wider set
  `{timed_out, cancelled, neutral, skipped, action_required, stale, startup_failure}`. This is
  **wider** than FR-010's explicitly-named four (`timed_out, cancelled, neutral, skipped`) — the
  spec names a minimum, and the classifier treats every non-`failure`, non-`success` conclusion
  as transient (conservative bias: anything not a definitive failure cannot anchor INHERITED).

**Reconciliation note.** The performer's `_FAILING_CONCLUSIONS`
(`{failure,timed_out,cancelled,action_required}`,
`agent/performer/src/performer/github.py:91`) groups timed_out/cancelled/action_required with
*failing* for its own pre-handoff gate — a deliberately different question ("did my checks
pass?") from the coordinare classifier's ("is this a *stable inherited* failure I may
auto-repair?"). The two partitions are intentionally distinct and both correct in context; no
performer change is needed. Rules are evaluated source-order, first-match-wins (Decision 3), so
a transient head conclusion is caught by rule 1 (FLAKE) before any signature comparison.

---

## Constitution Re-Check (post-research) ✅ PASS

- **I**: each decision adds one focused unit; the speculative `guard_lm_model` is explicitly
  dropped (Decision 5); base-rollup is a separate query/parser, not interface bloat (Decision 2).
- **II**: anti-masking corpus (Decision 1, SC-003), test-weakening corpus (Decision 5, SC-004),
  collision detection (Decision 1), and the exactly-one-classification invariant (Decision 3)
  are all named; FLAKE/transient dominance (cross-cutting) keeps flaky baselines out of repair.
- **III**: every escalation is a visible phase + comment (Decisions 4, 5); no new UX vocabulary.
- **IV**: every decision carries a measurable budget and names its benchmark; zero cost at
  defaults.
- **V**: every implementation-level clarification is resolved here — normalization regex set,
  16-char hash + collision, base GraphQL shape + base BPR + 075 path, classification table,
  config Option B, judge mechanism (diagnostic dispatch), approval invalidation (GitHub-native),
  stable-vs-transient set, L1 wiring. **Zero NEEDS CLARIFICATION remain.**
