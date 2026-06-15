# Data Model — Baseline Repair Autonomy (spec-090)

**Phase 1 output.** Derived from [research.md](research.md) (8 decisions) and the
entities enumerated in [spec.md](spec.md) §Key Entities. Every model delta below
is an **extension** of an existing model unless marked NEW. All new optional fields
default to a value that reproduces pre-feature behaviour byte-for-byte (SC-006).

This document is the authoritative field inventory for spec-090. The two
[contracts/](contracts/) files carry the cross-boundary **Field Registry** tables
(required by the speckit.analyze G-check); this file carries the in-process model
shapes, validation rules, and the migration story.

---

## 1. Failure signature (NEW) — `services/failure_signature.py`

A failure signature is the stable identity of a single failed check, computed from
its name, conclusion, and a **normalized failure reason**. It is the anti-masking
primitive: the same check failing for a *different reason* must produce a *different*
signature so it classifies as INTRODUCED, never INHERITED (FR-007, FR-009, SC-003).

This is not a persisted entity. It is a pure value produced at classification time and
carried inside `FailedCheckWithSignature` (§4) and the repair mandate (§7).

### Functions

```python
def normalize_reason(title: str | None, summary: str | None) -> str: ...
def make_failure_signature(
    name: str, conclusion: str, title: str | None, summary: str | None
) -> tuple[str, str]:  # returns (signature_hash, normalized_reason)
    ...
```

### Rules

| Rule | Detail |
|------|--------|
| Reason source | `title` primary; fall back to `summary` when title absent; `""` when both absent. |
| Normalization | lowercase; collapse runs of whitespace to a single space; strip volatile drift via module-load-compiled regexes. |
| Drift stripped | ISO-8601 timestamps→`<ts>`; durations (`1m3s`, `42ms`, `1.2s`)→`<dur>`; run/job/build IDs→`<id>`; 16+-char hex / SHAs→`<hex>`; UUIDs→`<uuid>`; paths-with-embedded-hashes→`<path>`; line/column suffixes (`:123:7`)→`:<n>`. |
| Hash | `hashlib.sha256(f"{name}\x1f{conclusion}\x1f{normalized_reason}".encode()).hexdigest()[:16]` — **16 chars**, matching the `compute_ci_gate_signature` precedent (`ci_gate.py:23-40`). |
| Determinism | No clock, no RNG, no environment reads. Same inputs → same outputs, always (Constitution II). |
| Collision handling | Two distinct `normalized_reason` values producing the same 16-char hash is detected at classification time by `ci_gate.compare_signatures()` and forces **UNKNOWN** (escalate, never INHERITED). |

### Performance

≤50µs per check; ≤5ms per rollup of typical fan-out (research Decision 1).

---

## 2. Check entry — `pr_checks_service.CheckEntry` (EXTEND, frozen)

The wire-parsed view of one check on a rollup. Today it carries no failure text, so a
signature cannot be computed. Add the two fields the GraphQL `output {}` block now
fetches (research Decision 1 Correction).

| Field | Type | Default | New? | Notes |
|-------|------|---------|------|-------|
| `name` | `str` | — | | existing |
| `status` | `str` | — | | existing |
| `conclusion` | `str \| None` | `None` | | existing |
| `is_required` | `bool` | `False` | | existing |
| `details_url` | `str \| None` | `None` | | existing |
| `title` | `str \| None` | `None` | **NEW** | CheckRun `output.title`; `None` for legacy/StatusContext rows |
| `summary` | `str \| None` | `None` | **NEW** | CheckRun `output.summary`; `None` when absent |

`frozen=True` is preserved. Both new fields are `None`-defaulted so existing
constructions and fixtures stay valid (the GraphQL fragment, `parse_rollup`, and the
rollup fixtures are updated alongside — research Decision 1).

---

## 3. Check rollup — `pr_checks_service.CheckRollup` (EXTEND, frozen)

| Field | Type | Default | New? | Notes |
|-------|------|---------|------|-------|
| `pr_number` | `int` | — | | existing |
| `head_sha` | `str` | — | | existing |
| `head_pushed_at` | … | — | | existing |
| `branch_protection_readable` | `bool` | — | | existing |
| `checks` | `tuple[CheckEntry, ...]` | — | | existing |
| `at_context_cap` | `bool` | `False` | | existing |
| `base_ref` | `str` | `""` | | existing |
| `rollup_origin` | `Literal["head", "base"]` | `"head"` | **NEW** | distinguishes a head rollup from a base-branch rollup (research Decision 2). Default `"head"` keeps every existing construction unchanged. |

A base rollup is produced by the NEW `parse_base_rollup()` / `get_base_branch_check_rollup()`
path and stamped `rollup_origin="base"`. `pr_number`/`head_pushed_at` are not meaningful for a
base rollup and are filled with neutral values (`0` / `None`); consumers key off `rollup_origin`.

---

## 4. Failed check with signature (NEW) — `services/ci_gate.py`

A failed check enriched with its head signature and (when comparable) the matching
baseline signature. Subclass of the existing `FailedCheck` (`ci_gate.py:43-56`,
`extra="forbid"`) so it serializes identically plus two fields.

```python
class FailedCheckWithSignature(FailedCheck):
    head_signature: str
    baseline_signature: str | None = None
```

| Field | Type | Default | New? | Notes |
|-------|------|---------|------|-------|
| `name` | `str` | — | | inherited from `FailedCheck` |
| `conclusion` | `str` | — | | inherited |
| `html_url` | `str \| None` | `None` | | inherited |
| `last_log_line` | `str \| None` | `None` | | inherited |
| `head_signature` | `str` | — | **NEW** | 16-char hash from `make_failure_signature` for the head check |
| `baseline_signature` | `str \| None` | `None` | **NEW** | matching base-branch signature when a same-name baseline failure exists; `None` otherwise |

---

## 5. Failure classification (NEW) — `services/failure_classification.py`

Pure classification of a single head failure against the baseline failure index.
No persisted state; produced fresh each cycle.

```python
Classification = Literal["inherited", "introduced", "flake", "unknown"]

def classify_failure_origin(
    head_check: FailedCheckWithSignature,
    baseline_index: dict[str, BaselineFailure],  # name -> baseline failure record
) -> Classification: ...
```

`BaselineFailure` is a small NEW value object (name, conclusion, signature) extracted
from the base rollup's failing checks.

### Decision table — SOURCE ORDER, first match wins (research Decision 3)

| # | Condition | Result |
|---|-----------|--------|
| 1 | head conclusion is transient (`_is_transient_conclusion`) | **FLAKE** |
| 2 | baseline indeterminate (no base rollup) **or** signature collision detected | **UNKNOWN** |
| 3 | same-name baseline failure exists but its conclusion is transient (flaky baseline) | **INTRODUCED** |
| 4 | baseline failure is stable **and** `baseline_signature == head_signature` | **INHERITED** |
| 5 | otherwise (no baseline failure, or stable baseline with a *different* signature) | **INTRODUCED** |

### Stable vs transient (cross-cutting, research)

| Bucket | Conclusions |
|--------|-------------|
| **Stable** (INHERITED-eligible) | exactly `{"failure"}` |
| **Transient** (never INHERITED → FLAKE/INTRODUCED) | `{"timed_out", "cancelled", "neutral", "skipped", "action_required", "stale", "startup_failure"}` |

This set is intentionally **wider** than FR-010's named four and is intentionally
distinct from the performer's `_FAILING_CONCLUSIONS` (which answers a different
question — "did *my* checks pass?"). No performer change.

### Performance

≤50ms p95 for a full rollup; pure CPU, no I/O (research Decision 3).

---

## 6. Gate decision — `ci_gate.CIGateDecision` (EXTEND, `extra="forbid"`)

The existing verdict object (`ci_gate.py:59-100`). Spec-090 adds four
classification lists and an observe-only invariant. **The verdict itself is
unchanged** in L1/L2 — classification is recorded, not acted on, until L3 is enabled.

| Field | Type | Default | New? | Notes |
|-------|------|---------|------|-------|
| `verdict` | `Verdict` | — | | existing (`pass`/`hold`/`bounce`/`escalate`) |
| `head_sha` | `str` | — | | existing |
| `required_checks` | `list[str]` | — | | existing |
| `failed_checks` | `list[FailedCheck]` | — | | existing |
| `pending_checks` | `list[str]` | — | | existing |
| `resolver_source` | `str` | — | | existing |
| `bounce_count_after` | `int` | — | | existing |
| `max_bounces_per_head` | `int` | `0` (ge=0) | | existing |
| `decided_at` | … | — | | existing |
| `inherited_checks` | `list[FailedCheckWithSignature]` | `Field(default_factory=list)` | **NEW** | head failures matching a stable baseline failure of the same signature |
| `introduced_checks` | `list[FailedCheckWithSignature]` | `Field(default_factory=list)` | **NEW** | head failures with no matching stable baseline signature |
| `flake_checks` | `list[FailedCheck]` | `Field(default_factory=list)` | **NEW** | head failures with a transient conclusion |
| `unknown_checks` | `list[FailedCheck]` | `Field(default_factory=list)` | **NEW** | baseline indeterminate or signature collision |

### Observe-only invariant (model_validator, `mode="after"`)

> **Exactly-one-classification.** When any classification list is non-empty, every
> `name` in `failed_checks` appears in **exactly one** of the four lists, and no
> classification list contains a name absent from `failed_checks`.

- When all four lists are empty (the default — L2 disabled), the validator is a no-op,
  guaranteeing existing decisions remain valid (SC-006).
- The validator **never** inspects or mutates `verdict`. L1/L2 ship without changing
  any merge/bounce/hold outcome.
- The existing `_validate_verdict_invariants` validator is unchanged and continues to run.

---

## 7. Repair mandate (NEW, transient payload) — coordinare → performer

Carried on the existing `JobInitPayload.metadata` channel under key `repair_mandate`
(research Decision 4). **No new transport field** — the performer already reads
`JobInitPayload.metadata`, so no performer code change is required. Full wire shape and
Field Registry: [contracts/repair-dispatch.md](contracts/repair-dispatch.md).

```jsonc
metadata["repair_mandate"] = {
  "type": "baseline_repair",
  "inherited_checks": [
    {"name": str, "conclusion": str, "normalized_reason": str, "html_url": str | null}
  ],
  "attempt": int,          // 1-based attempt number being dispatched
  "max_attempts": int,     // == max_repair_attempts_per_head
  "instruction": str       // explicitly forbids weakening/skipping/deleting/mocking-away/loosening tests
}
```

`normalized_reason` (not the raw title) is sent so the implementer sees the same
canonicalized reason the classifier matched on, and so no volatile drift leaks into the
mandate. The `instruction` string is the durable contract that the repair must fix
code/config, never neuter a test (Constitution II; FR-016).

---

## 8. Test-integrity guard verdict (NEW, transient) — `services/test_integrity_guard.py`

The static half of the dual guard (FR-019, FR-020). Pure function over a unified diff.

```python
def analyze_diff(diff: str) -> tuple[bool, list[str]]:
    # returns (is_safe, flagged_patterns)
```

| Aspect | Detail |
|--------|--------|
| `is_safe` | `True` only when the diff confidently contains no test-weakening change. Conservative: cannot confidently clear → `False`. |
| `flagged_patterns` | human-readable reasons (deleted assertion, loosened comparison operator, conditional/exception-suppressed assertion, added skip/xfail/disable, removed setup/teardown/fixture, mocked-away requirement). |
| Adversarial half | a separate `diagnostic`-role performer dispatch with fresh context (research Decision 5). Either half vetoing → reject + escalate. Uncertainty → reject (conservative). |
| Scope | the guard governs **land-as-candidate**, never **merge**. It never pushes; rejection sets `phase="blocked"`. |

The guard verdict gates the L3 land step in-process **and** is persisted as a
`RepairDecisionRecord` (§9, `kind="static_guard"` / `kind="reviewer"`) and mirrored to
observability — FR-023 requires every guard verdict to be auditable in persisted state,
not just consumed in-flight. The `diagnostic` reviewer dispatch is off the hot path.
Static budget ≤100ms p95 for a ≤2000-line diff (research Decision 5).

---

## 9. Repair attempt budget + decision audit — `state_store.PersistedSession` (EXTEND, v9)

A per-head counter of dispatched baseline-repair attempts, parallel to spec-075's
`bounce_counter` and spec-089's `local_fix_counter` (research Decision 6); plus an
append-only audit log of repair decisions that makes FR-023's persisted-audit clause
literal — **every** dispatch, guard verdict, acceptance/rejection, and escalation is
recorded in persisted state (the budget state is the counter itself). The same records
are also emitted to observability; the persisted copy is the durable audit trail.

```python
class RepairDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    head_sha: str
    attempt: int                               # 1-based; the dispatch this decision belongs to
    kind: Literal["dispatch", "static_guard", "reviewer",
                  "acceptance", "rejection", "escalation"]
    is_safe: bool | None = None                # set for static_guard / reviewer kinds
    flagged_patterns: list[str] = Field(default_factory=list)   # guard reasons, if any
    detail: str | None = None                  # escalation/rejection reason, free text
    decided_at: str                            # ISO-8601 stamp (set by the node, not a pure path)
```

| Field | Type | Default | Schema ver | Notes |
|-------|------|---------|------------|-------|
| `inheritance_repair_counter` | `dict[str, int]` | `Field(default_factory=dict)` | **v9** | `head_sha → attempts dispatched`. Read via `.get(head_sha, 0)`; **incremented at dispatch** (not at completion). This is FR-023's persisted **budget state**. |
| `repair_audit` | `list[RepairDecisionRecord]` | `Field(default_factory=list)` | **v9** | append-only audit of FR-023's `dispatch` / `guard verdicts` (`static_guard`, `reviewer`) / `acceptance` / `rejection` / `escalation` decisions; mirrored to observability. Empty for any pre-L3 / L3-disabled session (SC-006). |

### Migration

| Item | Value |
|------|-------|
| `CURRENT_SCHEMA_VERSION` | `8` → **`9`** (`state_store.py:18`) |
| `MIN_SUPPORTED_SCHEMA_VERSION` | unchanged (`1`) |
| Forward-migration of v1–v8 snapshots | inject `inheritance_repair_counter = {}` **and** `repair_audit = []`; comment block (`state_store.py:20-38`) gains a v9 line. |
| Empty ≠ unlimited | a missing/empty counter means **zero attempts taken**, not "no budget" — the configured `max_repair_attempts_per_head` still applies (FR-026, SC-009). |
| Field placement | immediately after `local_fix_counter` (`state_store.py:114`). |

### Performance

≤1ms added to a v8 snapshot load (research Decision 6).

---

## 10. Repair gate config — `config.py` (NEW classes, EXTEND `PersonaScopeConfig`)

Three independent, default-disabled classes — one per layer — so the phased rollout
can enable layers in isolation (research Decision 6, Option B; coupling L1/L2 in one
class was rejected because it breaks SC-006 phased isolation). All three use
`model_config = ConfigDict(extra="forbid")`.

```python
class BaselinePreventionGateConfig(BaseModel):       # L1
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False

class BaselineClassificationGateConfig(BaseModel):   # L2
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False

class InheritedRepairGateConfig(BaseModel):          # L3
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_repair_attempts_per_head: int = Field(default=1, ge=0, le=20)
```

| Class | Field | Type | Default | Notes |
|-------|-------|------|---------|-------|
| `BaselinePreventionGateConfig` | `enabled` | `bool` | `False` | L1 merge-precondition gate |
| `BaselineClassificationGateConfig` | `enabled` | `bool` | `False` | L2 observe-only classification |
| `InheritedRepairGateConfig` | `enabled` | `bool` | `False` | L3 autonomous repair |
| `InheritedRepairGateConfig` | `max_repair_attempts_per_head` | `int` | `1` (ge=0, le=20) | `0` = classify/observe but never dispatch; budget always applies |

### `PersonaScopeConfig` (EXTEND, after `local_test_gate` ~`config.py:1371`)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `baseline_prevention_gate` | `BaselinePreventionGateConfig` | `Field(default_factory=BaselinePreventionGateConfig)` | per-persona-scope L1 toggle |
| `baseline_classification_gate` | `BaselineClassificationGateConfig` | `Field(default_factory=BaselineClassificationGateConfig)` | per-persona-scope L2 toggle |
| `inherited_repair_gate` | `InheritedRepairGateConfig` | `Field(default_factory=InheritedRepairGateConfig)` | per-persona-scope L3 toggle |

With all three defaulted off, a config that omits them validates and behaves exactly as
pre-spec-090 (SC-006). `extra="forbid"` ensures typos in the new blocks are load-time
errors, not silent no-ops. Budget: ≤1ms over a v8 config load (research Decision 6).

---

## 11. Base-gate decision — `services/base_gate.py` (NEW, L1)

The L1 merge-precondition gate (US1) resolves into a small, frozen decision object so the
`monitor_pr.py` wiring branches on a typed `decision` literal instead of a bare bool, and
so the offending **base** check(s) can be named in the hold record (FR-003) distinctly
from any head failure. The object is **in-process only** — it gates the L1 merge
transition and is not persisted; the existing hold/observability records carry its content.

```python
class BaseGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["BLOCK", "PROCEED", "INDETERMINATE"]
    failing_checks: list[FailedCheck] = Field(default_factory=list)  # base checks only

def evaluate_base_gate(base_rollup, scope, ...) -> BaseGateDecision: ...
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `decision` | `Literal["BLOCK","PROCEED","INDETERMINATE"]` | — | BLOCK = required base check red (hold); PROCEED = base green or only non-required red; INDETERMINATE = base rollup `None` (fail-safe fall-through to head-only, **never** BLOCK) |
| `failing_checks` | `list[FailedCheck]` | `Field(default_factory=list)` | the offending **base** required check(s) named in the hold (FR-003); empty unless `decision == "BLOCK"` |

`evaluate_base_gate` resolves the base required set via `required_checks_resolver.resolve`
and decides via `pr_checks_policy.decide` over the base rollup (reusing the spec-064
machinery). A `None` base rollup short-circuits to `INDETERMINATE` (FR-005, SC-002). The
gate is **re-read each cycle and never latched** (FR-006): the same red base holds again,
and a base that turns green proceeds on the next evaluation. Pure given its inputs (the
network fetch happens upstream in `get_base_branch_check_rollup`), so unit tests are
deterministic. Budget: the enabled gate adds ≤500ms p95 with head+base fetched
concurrently (research Decision 2/8); disabled it adds nothing (SC-006).

---

## Field-origin cross-reference

| Entity (spec.md §Key Entities) | Model home | Section |
|--------------------------------|-----------|---------|
| Base-branch check state | `CheckRollup` (`rollup_origin="base"`) | §3 |
| Base-gate decision (L1) | `services/base_gate.py` `BaseGateDecision` | §11 |
| Failure signature | `services/failure_signature.py` | §1 |
| Failure classification | `services/failure_classification.py` + `CIGateDecision` lists | §5, §6 |
| Gate decision (extended) | `CIGateDecision` | §6 |
| Repair attempt budget | `PersistedSession.inheritance_repair_counter` | §9 |
| Repair decision audit | `PersistedSession.repair_audit` (`RepairDecisionRecord`) | §9 |
| Test-integrity guard verdict | `services/test_integrity_guard.py` | §8 |
| Repair gate config | three config classes on `PersonaScopeConfig` | §10 |
| Repair mandate (dispatch payload) | `JobInitPayload.metadata["repair_mandate"]` | §7 |

## Determinism & coverage notes (Constitution II)

- `failure_signature`, `failure_classification`, `test_integrity_guard` (static),
  `pr_checks_policy.decide`, and `required_checks_resolver.resolve` are all pure —
  no clock, RNG, or network — so unit tests are deterministic by construction.
- New code lands with tests in the same change (no coverage decrease): a normalization
  corpus + anti-masking corpus (SC-003) for §1/§5, a test-weakening corpus (SC-004) for
  §8, a v8→v9 migration test for §9, and a defaults-inert test for §10/§6 (SC-006).
