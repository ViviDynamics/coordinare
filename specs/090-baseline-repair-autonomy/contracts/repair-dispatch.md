# Contract — Repair Dispatch (coordinare → performer)

**Boundary:** coordinare (`monitor_performer.py` L3 dispatch) → performer (Job API).
**Transport:** the existing `JobInitPayload.metadata` dict — **no new transport field**.
The performer already reads `JobInitPayload.metadata`, so this contract adds **zero**
performer code change (research Decision 4).

When L3 (`inherited_repair_gate`) is enabled, an INHERITED stable failure is in budget,
and dispatch is chosen, coordinare sets a single key on the outgoing job's metadata:

```jsonc
metadata["repair_mandate"] = {
  "type": "baseline_repair",
  "inherited_checks": [
    {
      "name": "ci / unit-tests (3.12)",
      "conclusion": "failure",
      "normalized_reason": "assertionerror: expected <n> rows got <n>",
      "html_url": "https://github.com/<org>/<repo>/runs/<id>"
    }
  ],
  "attempt": 1,
  "max_attempts": 1,
  "instruction": "A check that is already red on the base branch is failing on this PR for the same reason. Fix the underlying code or configuration so the check passes. You MUST NOT weaken, skip, xfail, delete, comment-out, mock-away, or loosen any test or assertion to make the check pass. If the only way to make it pass is to change a test's strictness, stop and leave the work for a human."
}
```

The `instruction` string is the durable, in-payload contract that survives long runs
(the model may forget earlier turns): the repair must fix code/config, never neuter a
test (FR-016, Constitution II). The coordinare-side classifier sends `normalized_reason`
(not the raw GitHub title) so the implementer sees exactly the canonicalized reason the
classifier matched on, with all volatile drift already stripped (§failure_signature).

The mandate is purely additive: when L3 is disabled or no INHERITED failure is in budget,
`metadata` carries no `repair_mandate` key and dispatch is byte-identical to today (SC-006).

## Field Registry

All fields below live under `metadata["repair_mandate"]`. The G-check (speckit.analyze)
reconciles these names against payload-change language in spec.md / plan.md.

| Field | Type | Required | Origin | Consumed by | Notes |
|-------|------|----------|--------|-------------|-------|
| `repair_mandate` | object | yes (only when L3 dispatches) | coordinare `monitor_performer.py` | performer Job API → implementer persona | top-level key on existing `JobInitPayload.metadata`; absent ⇒ no baseline-repair job |
| `type` | string | yes | coordinare | performer | discriminator; always `"baseline_repair"` for this contract |
| `inherited_checks` | array<object> | yes | coordinare classifier (`failure_classification`) | implementer persona | the INHERITED failures this attempt must fix; non-empty |
| `inherited_checks[].name` | string | yes | GitHub check name (head rollup) | implementer | exact check name as reported by GitHub |
| `inherited_checks[].conclusion` | string | yes | head `CheckEntry.conclusion` | implementer | stable conclusion (always `"failure"` for INHERITED) |
| `inherited_checks[].normalized_reason` | string | yes | `failure_signature.normalize_reason` | implementer | canonicalized failure reason; drift stripped; `""` if no text available |
| `inherited_checks[].html_url` | string \| null | no | head `CheckEntry.details_url` | implementer | deep link to the failing run; `null` when GitHub omits it |
| `attempt` | integer | yes | `inheritance_repair_counter` (incremented at dispatch) | implementer / observability | 1-based number of the attempt being dispatched |
| `max_attempts` | integer | yes | `InheritedRepairGateConfig.max_repair_attempts_per_head` | implementer / observability | per-head budget ceiling; `attempt ≤ max_attempts` is enforced before dispatch |
| `instruction` | string | yes | coordinare (static template) | implementer persona | durable do-not-weaken-tests mandate (FR-016) |

## Invariants

- **Additive-only.** No field of `JobInitPayload` is renamed or removed; `repair_mandate`
  is an optional metadata key. Absent ⇒ pre-feature behaviour (SC-006).
- **Budget before dispatch.** `attempt` is the post-increment value of
  `inheritance_repair_counter[head_sha]` and is always `≤ max_attempts`; exhaustion
  escalates (`phase="blocked"`) instead of dispatching (FR-022, FR-023).
- **Reason fidelity.** `normalized_reason` equals the value the classifier hashed into the
  INHERITED match — the implementer and the gate reason about the same canonical string.
- **No test-weakening.** Whatever the implementer produces is re-checked by the dual
  test-integrity guard before it can land as a candidate (see
  [gate-decision.md](gate-decision.md) and data-model.md §8); the `instruction` is the
  human-readable half of that contract.
