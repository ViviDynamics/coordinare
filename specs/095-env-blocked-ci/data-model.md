# Phase 1 Data Model: ENV_BLOCKED CI-Failure Classification

Extends the spec-090 classification substrate. One new classification value, one new config block, one new per-card state field, one pure matcher type. No change to 090's existing inherited/introduced/flake/unknown logic.

## Extended entity

### Classification (`services/failure_classification.py`)
Existing: `Literal["inherited", "introduced", "flake", "unknown"]`.
**Add** `"env_blocked"` → `Literal["env_blocked", "inherited", "introduced", "flake", "unknown"]`.
`classify_failure_origin(...)` gains a **Row 0**: if `match_env_signature(head_reason, patterns)` returns a cause → `env_blocked` (short-circuit, before all existing rows). Evaluated on the HEAD failure alone (no baseline dependency, FR-011).

## New entities

### EnvSignaturePattern (`config.py`)
An operator-/built-in infra pattern. Pydantic, `extra="forbid"`.
- `id: str` — stable identifier (e.g. `artifact_storage_quota`).
- `regex: str` — matched against the normalized reason (case-insensitive; reason is already lowercased).
- `cause: str` — human-readable cause ("CI artifact-storage quota exhausted").
- `action: str` — operator action ("Raise the Actions storage budget or clear old artifacts").

### EnvBlockedGateConfig (`config.py`, on `PersonaScopeConfig`)
Mirrors the 090 gates. Pydantic, `extra="forbid"`.
- `enabled: bool = False` — default-off (FR-012, SC-006).
- `patterns: list[EnvSignaturePattern] = []` — operator additions, matched **in addition to** the built-in defaults (quota / runner-offline / billing-limit) that ship in the matcher.
Resolved per-symphony via a `_get_env_blocked_gate_config(state)` helper paralleling `_get_baseline_classification_gate_config`.

### EnvCause (`services/env_signature.py`, return of `match_env_signature`)
Result of a match (or `None` for no match → fail-safe fallthrough, FR-003).
- `pattern_id: str`, `cause: str`, `action: str` — carried into state + the operator notification.

### EnvBlockedState (per-card, on `PersistedSession` in `state_store.py`)
Durable per-card hold + dedup state. Optional; old snapshots default `None` (schema-version bump).
- `pattern_id: str` — which infra condition is blocking (basis for dedup + clear comparison).
- `cause: str`, `action: str` — for the operator surface.
- `notified_at: datetime | None` — set when the operator was notified (dedup: notify once per condition, FR-006).
Set when classification returns `env_blocked`; **cleared to `None`** when a later evaluation yields no env match for the card (auto-resume, FR-008). Per-card so one card's block never suppresses another's.

## State transitions (per card)

| Current | Event | Next |
|---|---|---|
| no env_blocked | required check matches an infra signature | `env_blocked` set (pattern_id, cause, action); operator notified once; **held** — no repair mandate, no re-dispatch (FR-004) |
| `env_blocked` (same pattern) | re-evaluated, signature still matches | held; **no re-notify** (notified_at already set, FR-006) |
| `env_blocked` | re-evaluated, no infra signature matches | cleared to `None`; **normal flow resumes** (classify/dispatch, FR-008) |
| `env_blocked` on check A + introduced failure on check B | re-evaluated | held on A; B still classified INTRODUCED and surfaced (not masked, FR-009) |
| any | feature disabled | no env_blocked ever; behavior identical to pre-feature (FR-012, SC-006) |

## Observability (secret-free — FR-010)

- Classification event gains `env_blocked` as a possible value with `pattern_id`.
- A distinct operator notification (`notify.py`): names `cause` + `action`, deduped on `notified_at`. Fields: card identifier, check name, conclusion, normalized reason, pattern_id, cause, action — **never** secret values.
