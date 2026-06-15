# Contract — Gate Decision serialization (extended `CIGateDecision`)

**Boundary:** in-process producer (`monitor_performer._evaluate_ci_gate`) → consumers
(routing, observability capture, persisted snapshot). The decision object is also the
shape emitted to the job's `capture_dir` for L2 observe-only analysis.

Spec-090 extends the existing `CIGateDecision` (`ci_gate.py:59-100`, `extra="forbid"`)
with four classification lists, extends `FailedCheck` with a signature-bearing subclass,
and extends the upstream `CheckEntry`/`CheckRollup` rows that feed it. **The `verdict`
field is unchanged** — L1/L2 record classification but never alter merge/bounce/hold
outcomes (SC-006).

## Extended decision (illustrative serialization)

```jsonc
{
  "verdict": "hold",                  // UNCHANGED by L1/L2
  "head_sha": "abc123…",
  "required_checks": ["ci / unit-tests (3.12)"],
  "failed_checks": [ { "name": "ci / unit-tests (3.12)", "conclusion": "failure",
                       "html_url": "https://…", "last_log_line": "AssertionError…" } ],
  "pending_checks": [],
  "resolver_source": "branch_protection",
  "bounce_count_after": 0,
  "max_bounces_per_head": 3,
  "decided_at": "2026-06-13T…",

  // NEW — classification lists (all default [] ⇒ omitted/empty when L2 disabled)
  "inherited_checks": [
    { "name": "ci / unit-tests (3.12)", "conclusion": "failure",
      "html_url": "https://…", "last_log_line": "AssertionError…",
      "head_signature": "9f3a1c20b7d4e6f8",
      "baseline_signature": "9f3a1c20b7d4e6f8" }
  ],
  "introduced_checks": [],
  "flake_checks": [],
  "unknown_checks": []
}
```

## Field Registry

| Field | Type | Required | Origin | Consumed by | Notes |
|-------|------|----------|--------|-------------|-------|
| `inherited_checks` | array<FailedCheckWithSignature> | no (default `[]`) | `failure_classification.classify_failure_origin` | routing/L3, observability | head failure matching a stable baseline failure of equal signature |
| `introduced_checks` | array<FailedCheckWithSignature> | no (default `[]`) | classifier | observability | head failure with no matching stable baseline signature (incl. baseline-different-reason) |
| `flake_checks` | array<FailedCheck> | no (default `[]`) | classifier | observability | head failure with a transient conclusion |
| `unknown_checks` | array<FailedCheck> | no (default `[]`) | classifier | observability | baseline indeterminate or signature collision → escalate, never INHERITED |
| `head_signature` | string | yes (within `FailedCheckWithSignature`) | `make_failure_signature` (head) | classifier, L3 mandate | 16-char sha256 hash; matches `compute_ci_gate_signature` width |
| `baseline_signature` | string \| null | no (default `null`) | `make_failure_signature` (base rollup) | classifier | present when a same-name baseline failure exists; equality with `head_signature` ⇒ INHERITED |
| `rollup_origin` | string (`"head"`\|`"base"`) | no (default `"head"`) | `pr_checks_service` parse path | classifier | distinguishes head vs base `CheckRollup`; base rollup feeds the baseline index |
| `title` | string \| null | no (default `null`) | GitHub CheckRun `output.title` | `make_failure_signature` | NEW on `CheckEntry`; primary reason source |
| `summary` | string \| null | no (default `null`) | GitHub CheckRun `output.summary` | `make_failure_signature` | NEW on `CheckEntry`; fallback reason source |

`FailedCheckWithSignature` also carries the four inherited `FailedCheck` fields
(`name`, `conclusion`, `html_url`, `last_log_line`) unchanged.

## Invariants

- **Verdict untouched.** No spec-090 field influences `verdict`; the existing
  `_validate_verdict_invariants` validator is unchanged (SC-006).
- **Exactly-one-classification (observe-only).** When any classification list is
  non-empty, every `failed_checks[].name` appears in exactly one of the four lists, and
  no classification list names a check absent from `failed_checks` (data-model.md §6). A
  fully-empty set of lists (L2 disabled) is valid and skips the invariant.
- **Defaults reproduce baseline.** All four lists default to `[]` and all new
  `CheckEntry`/`FailedCheckWithSignature`/`CheckRollup` fields default to `null`/`"head"`,
  so a decision produced with L1/L2/L3 disabled serializes identically to a pre-spec-090
  decision modulo the (empty) list keys (SC-006).
- **Signature width is fixed.** `head_signature`/`baseline_signature` are exactly 16 hex
  chars; a same-reason match requires byte-equal strings. Distinct reasons colliding to
  the same 16-char hash are detected (`ci_gate.compare_signatures`) and routed to
  `unknown_checks`.
