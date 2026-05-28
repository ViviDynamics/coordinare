# Contract: CI Gate Decision

**Branch**: `075-implementer-ci-gate` | **Plan**: [../plan.md](../plan.md) | **Data model**: [../data-model.md](../data-model.md)

This contract pins the externally observable surface of the gate: the decision shape returned to the graph, the relay-feedback entries delivered to the implementer, and the PR rollup comment posted by `notify.py`. Tests under `tests/contract/test_gate_decision_schema.py` enforce these shapes.

---

## 1. `CIGateDecision` (returned by gate eval)

```json
{
  "verdict": "bounce",
  "head_sha": "a1b2c3d4e5f6...4040char",
  "required_checks": ["integration-tests", "lint", "unit-tests"],
  "failed_checks": [
    {
      "name": "unit-tests",
      "conclusion": "failure",
      "html_url": "https://github.com/org/repo/actions/runs/12345",
      "last_log_line": "FAILED tests/unit/test_foo.py::test_bar - AssertionError: expected 3, got 5"
    }
  ],
  "pending_checks": [],
  "resolver_source": "persona_check_map",
  "bounce_count_after": 1,
  "decided_at": "2026-05-28T14:23:11.482Z"
}
```

**Field rules**:

- `verdict` ∈ `{pass, hold, bounce, escalate}`.
- `head_sha` is a full 40-char Git SHA, lowercase hex.
- `required_checks` is sorted ASCII ascending for stable signature.
- `failed_checks` is empty when `verdict ∈ {pass, hold}`; non-empty when `verdict ∈ {bounce, escalate}`.
- `pending_checks` is non-empty only when `verdict == "hold"`; empty otherwise.
- `resolver_source` records which fallback layer produced the required-checks set — pure observability, never affects behavior.
- `bounce_count_after` is the value of `bounce_counter[head_sha]` AFTER this decision is applied; `0` for `pass`/`hold`.
- `decided_at` is ISO-8601 UTC with milliseconds.
- `last_log_line` is best-effort (`None` when unavailable); truncated to ≤200 characters when present.

---

## 2. `relay_feedback` entry shape (BOUNCE only)

When `verdict == "bounce"`, the gate appends a single dict to `state["relay_feedback"]`:

```json
{
  "body": "CI gate: 1 required check(s) failing on this HEAD (unit-tests). Fix and push before re-handing off to reviewer.",
  "author_login": "coordinare"
}
```

**Field rules**:

- `body` is a human-readable string summarising the failing checks; the implementer prompt renders it verbatim.
- `author_login` is always the literal `"coordinare"` so the `relay_feedback.py` dispatcher and the implementer prompt can identify gate-generated entries by sender rather than a `source` field.

This shape is the standard relay_feedback entry shape consumed by the existing `relay_feedback.py` dispatcher (spec 070) — no change to that node is required.  The richer CI decision details (`head_sha`, `failed_checks`, `bounce_count_after`, etc.) are available on `session["latest_ci_gate_decision"]` for tooling that needs them.

---

## 3. PR rollup comment template

Posted by `notify.py` on first decision per `(head_sha, decision-signature)` — re-posted only when the signature changes.

```markdown
<!-- coordinare:ci-gate:{decision_signature} -->
### Coordinare CI gate — {verdict_uppercase}

**HEAD**: `{head_sha_short}` &nbsp; **Required checks**: {len(required_checks)} ({resolver_source})

{verdict-specific block:}

— **pass**: "All required checks green. Advancing to reviewer."

— **hold**: "Waiting on pending checks: <ul><li>name</li>...</ul>"

— **bounce**: "Failing checks (bounce {n}/{max}):"
  <table><thead><tr><th>Check</th><th>Conclusion</th><th>Last line</th></tr></thead>
  <tbody><tr><td><a href="{html_url}">{name}</a></td><td>{conclusion}</td><td><code>{last_log_line}</code></td></tr></tbody></table>

— **escalate**: "Bounce limit reached ({max}/{max}) on this HEAD. Card moved to needs_human_review."

<details><summary>Decision details</summary>

- Verdict: `{verdict}`
- Required checks: `{', '.join(required_checks)}`
- Resolver source: `{resolver_source}`
- Decided at: `{decided_at}`

</details>
```

**Dedup**:

- The HTML comment `<!-- coordinare:ci-gate:{decision_signature} -->` is the dedup marker.
- On each post, scan existing PR comments for `<!-- coordinare:ci-gate:` prefix; if the signature matches an existing comment, skip; if it differs, post a new comment (do not edit-in-place — the audit trail of distinct decisions is the point).
- Dedup logic mirrors 074's `_persona_scope_signature` pattern.

---

## 4. Backward-compatibility guarantees

- `pr_checks_policy.decide()` signature gains an optional `required_check_names: set[str] | None = None`. Existing 064 callers omit the argument and retain identical behavior — verified by `test_pr_checks_policy.py` regression suite (no changes required there).
- `PersistedSession` v4 snapshots deserialize cleanly into v5 with `bounce_counter={}` (additive default).
- `relay_feedback.py` accepts entries with new `source` values without modification — existing entries (reviewer / human) keep their shape.

---

## 5. Test enforcement

- `tests/contract/test_gate_decision_schema.py` — round-trip `CIGateDecision` through JSON, assert all field rules, assert dedup signature stability across `decided_at` and other irrelevant changes.
- `tests/contract/test_persona_check_map_schema.py` — load YAML examples (valid + invalid), assert pydantic parsing matches spec.
- `tests/unit/graph/nodes/test_notify_ci_gate_rollup.py` — assert dedup marker presence, assert no duplicate posts on identical signature, assert new post on signature change.
