# Phase 1 Data Model: Implementer CI Gate

**Branch**: `075-implementer-ci-gate` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Entities

### `CIGateDecision` (in-memory, not persisted)

The output of the gate evaluation at the implementer→reviewer boundary. Returned by `monitor_performer`'s gate-eval block; consumed by the routing logic and the `notify.py` rollup.

```python
from typing import Literal, TypedDict

class FailedCheck(TypedDict):
    name: str                          # GitHub check name, e.g. "lint" or "unit-tests"
    conclusion: str                    # one of {failure, cancelled, timed_out, action_required, stale, startup_failure}
    html_url: str | None               # link to the check run (None if unavailable)
    last_log_line: str | None          # best-effort tail of the check log (truncated to 200 chars)

class CIGateDecision(TypedDict):
    verdict: Literal["pass", "hold", "bounce", "escalate"]
    head_sha: str                      # the HEAD the decision was evaluated against
    required_checks: list[str]         # the resolved required-check name set (sorted, for stable signatures)
    failed_checks: list[FailedCheck]   # populated only when verdict in {bounce, escalate}; empty otherwise
    pending_checks: list[str]          # check names still in_progress / queued; populated when verdict == "hold"
    resolver_source: Literal["persona_check_map", "branch_protection", "all_head_checks"]
    bounce_count_after: int            # counter[head_sha] AFTER this decision is applied (0 for pass/hold)
    decided_at: str                    # ISO-8601 UTC
```

**Signature for dedup**: `sha256(f"{head_sha}|{verdict}|{','.join(required_checks)}|{','.join(c['name'] for c in failed_checks)}")[:16]` — stable across cycles, changes when verdict or failure set changes.

---

### `BounceCounter` (persisted on `CardSession`)

Tracks how many times the gate has bounced the card back to implementer **per HEAD SHA**. Stored as a dict so the audit trail survives across HEADs.

```python
# session.py TypedDict extension
class CardSession(TypedDict, total=False):
    # ... existing fields ...
    bounce_counter: dict[str, int]     # head_sha -> bounce count for that HEAD
```

**Invariants**:

- Empty dict (`{}`) is the default and means "no bounces ever."
- Keys are full 40-char Git SHAs.
- A new HEAD does **not** remove prior entries — operators may inspect the history.
- The escalation check is `counter.get(current_head_sha, 0) >= max_bounces_per_head`.
- Round-trips through `_SESSION_FIELDS` (canonical pattern; regression test required per 073/074 drift history).

**Persistence**: schema v4→v5 on `PersistedSession`:

```python
class PersistedSessionV5(BaseModel):
    # ... v4 fields ...
    bounce_counter: dict[str, int] = Field(default_factory=dict)
```

v4 snapshots load with `bounce_counter={}` (additive default). MIN schema version unchanged.

---

### `RequiredChecksList` (in-memory, not persisted)

The output of `RequiredChecksResolver.resolve(...)`. A `set[str]` plus the source label that produced it:

```python
class RequiredChecksList(TypedDict):
    names: list[str]                   # sorted; only checks that exist on current HEAD
    source: Literal["persona_check_map", "branch_protection", "all_head_checks"]
```

Sorted to give `CIGateDecision.required_checks` a stable order for the dedup signature.

---

### Config: `CIGateConfig` and `PersonaCheckMapConfig`

Nested under existing `symphony.persona_scope.*` block (074) to keep the config surface coherent — operators who already opted into persona scoping pick this up by adding `persona_check_map` and the gate top-level. Operators on bare 064-only deployments use branch-protection fallback automatically.

```python
class PersonaCheckMapPerDepth(BaseModel):
    skim:   list[str] = Field(default_factory=list)   # glob patterns
    normal: list[str] = Field(default_factory=list)
    full:   list[str] = Field(default_factory=list)
    # 'skip' is not in this table — skip means no gate for that persona

class PersonaCheckMapConfig(RootModel[dict[str, PersonaCheckMapPerDepth]]):
    # persona name -> per-depth pattern lists
    pass

class CIGateConfig(BaseModel):
    enabled: bool = False
    max_bounces_per_head: int = Field(default=3, ge=1, le=20)
    pending_timeout_seconds: int = Field(default=900, ge=60, le=7200)   # delegates to 064's decide()
    # When True, gate evaluates at the implementer→reviewer boundary. When False, lifecycle advance is unchanged.

class PersonaScopeConfig(BaseModel):   # extending existing 074 model
    # ... existing 074 fields ...
    persona_check_map: PersonaCheckMapConfig | None = None
    ci_gate: CIGateConfig = Field(default_factory=CIGateConfig)
```

**YAML example**:

```yaml
symphony:
  persona_scope:
    enabled: true
    persona_check_map:
      implementer:
        skim:   ["lint*"]
        normal: ["lint*", "unit*"]
        full:   ["lint*", "unit*", "integration*"]
    ci_gate:
      enabled: true
      max_bounces_per_head: 3
      pending_timeout_seconds: 900
```

**Default-off**: `ci_gate.enabled=False` by default. Adoption is opt-in (mirrors 074's `persona_scope.enabled` posture, FR-014 / FR-009).

---

## State transitions

```
implementer marks itself terminal-success
              │
              ▼
       [gate evaluation]
              │
        ┌─────┼─────┬─────────┐
        ▼     ▼     ▼         ▼
       PASS  HOLD  BOUNCE  ESCALATE
        │     │     │         │
        │     │     │         └─► set phase=needs_human_review, write notify rollup
        │     │     └─► increment bounce_counter[head_sha], append relay_feedback, return HOLD phase
        │     └─► return {"phase": "monitoring_performer"} (no advance, no relay)
        └─► _advance_stage(state, status) → phase=reviewing
```

**HOLD vs BOUNCE distinction**:
- HOLD = checks still running. No state change beyond staying in monitoring_performer.
- BOUNCE = checks have failed. Counter increments, relay_feedback delivered to implementer on next turn, lifecycle stays in monitoring_performer.

This matches FR-002 (pending → wait) and FR-003 (failed → bounce) cleanly.

---

## Persistence schema bump

`state_store.py` schema version: `CURRENT = 5`, `MIN = 1` unchanged.

```python
def _migrate_v4_to_v5(payload: dict) -> dict:
    payload.setdefault("bounce_counter", {})
    return payload
```

Migration is a single line — the field is purely additive and the empty default is well-defined.

---

## Test surface

| Test | File | What it asserts |
|---|---|---|
| Resolver layer 1 (persona_check_map) | `test_required_checks_resolver.py` | Glob patterns from `implementer.normal` intersect correctly with HEAD checks |
| Resolver layer 2 (branch protection) | `test_required_checks_resolver.py` | Falls through when persona_check_map empty / disabled |
| Resolver layer 3 (all-checks) | `test_required_checks_resolver.py` | Final fallback when both prior layers empty |
| Gate PASS | `test_monitor_performer_ci_gate.py` | All required checks success → advance to reviewing |
| Gate HOLD | `test_monitor_performer_ci_gate.py` | Any required check pending → return monitoring_performer |
| Gate BOUNCE | `test_monitor_performer_ci_gate.py` | Failed required check → counter++, relay_feedback appended, monitoring_performer |
| Gate ESCALATE | `test_monitor_performer_ci_gate.py` | counter[head] >= max → needs_human_review |
| Counter reset semantics | `test_monitor_performer_ci_gate.py` | New HEAD → counter[new_head]==0, prior entries preserved |
| Fail-open | `test_monitor_performer_ci_gate.py` | Resolver raises → log + advance (no block) |
| Session round-trip | `test_session.py` | `bounce_counter` survives CardSession ↔ state ↔ PersistedSession ↔ disk |
| Dedup signature | `test_notify_ci_gate_rollup.py` | Same (head_sha, verdict, required, failed) → same sig; any change → different |
| Config schema | `test_persona_check_map_schema.py` | YAML parse, defaults, validation |
| Gate-decision schema | `test_gate_decision_schema.py` | CIGateDecision shape contract |
| E2E | `test_implementer_ci_gate_e2e.py` | red-CI → bounce → fix-push → green → reviewer |
