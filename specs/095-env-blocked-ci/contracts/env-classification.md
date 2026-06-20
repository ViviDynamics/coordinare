# Contract: ENV_BLOCKED Classification & Surface

No external API surface. Contracts are (1) the classification behavior, (2) the env-signature matcher, (3) the operator-notification shape. All verified by unit tests.

## Field Registry

No fields added to any cross-boundary dispatch payload or performer contract. Internal additions only: the `Classification` literal gains `env_blocked`; `PersistedSession` gains an optional `env_blocked` state field; `PersonaScopeConfig` gains `env_blocked_gate`. (The `/speckit.analyze` contract check is a no-op for 095 — no dispatch-payload field change.)

## Behavioral contract

### `match_env_signature(reason, patterns) -> EnvCause | None` (services/env_signature.py)
- C1: Returns an `EnvCause` (pattern_id, cause, action) when `reason` matches a built-in or configured pattern; first match wins. (FR-002)
- C2: Returns `None` when no pattern matches — never guesses. (FR-003 fail-safe)
- C3: Pure and deterministic; case-insensitive over the already-normalized reason; no I/O.

### `classify_failure_origin(...)` (extended)
- C4: A required head failure whose normalized reason matches an infra signature is classified `env_blocked`, evaluated **before** flake/inherited/introduced (Row 0). (FR-001)
- C5: A head failure matching no infra signature is classified by the existing 090 rows, unchanged. (FR-003)
- C6: `env_blocked` is determined from the HEAD failure alone — independent of baseline fetch state. (FR-011)

### Gate wiring (monitor_performer.py)
- C7: An `env_blocked` card builds **no** L3 repair mandate and triggers **no** performer re-dispatch/bounce — it is held. (FR-004)
- C8: The operator is notified once per condition (deduped on `env_blocked.notified_at`), with cause + action, distinct from a generic test-failure message, secret-free. (FR-005, FR-006, FR-010)
- C9: When a later evaluation yields no env match, the hold clears and normal flow resumes within one cycle, no manual reset. (FR-008)
- C10: An env_blocked check on a card does not mask an INTRODUCED failure on another check of the same card. (FR-009)
- C11: With the gate disabled, classification/routing/verdicts/notifications are byte-identical to the pre-feature baseline. (FR-012)

## Test contract (unit, deterministic)

| Test | Asserts | Maps |
|---|---|---|
| `test_match_artifact_quota_signature` | "artifact storage quota has been hit" / "createartifact … quota" → EnvCause(artifact_storage_quota) | C1 / FR-002 |
| `test_match_runner_and_billing_signatures` | runner-offline + spending-limit reasons → matched | C1 / FR-002 |
| `test_non_infra_reason_no_match` | a normal test-failure reason → None (no false positive) | C2 / FR-003 / SC-005 |
| `test_env_blocked_takes_priority_over_inherited` | #159 case: stable failure also failing on baseline + infra reason → `env_blocked`, not `inherited` | C4 / US1 / SC-007 |
| `test_non_infra_failure_falls_through` | non-infra failure → existing 090 classification unchanged | C5 / FR-003 |
| `test_env_blocked_independent_of_baseline` | indeterminate baseline + infra reason → still `env_blocked` | C6 / FR-011 |
| `test_env_blocked_holds_no_repair_no_redispatch` | env_blocked → no repair mandate, no re-dispatch | C7 / US1 / SC-001 |
| `test_env_blocked_notifies_once_with_cause_action` | one operator notification, cause+action, secret-free; re-eval same condition → no re-notify | C8 / US2 / SC-002,SC-003 |
| `test_env_blocked_auto_resumes_when_signature_clears` | next eval no infra match → hold cleared, normal flow resumes | C9 / US3 / SC-004,SC-008 |
| `test_env_block_does_not_mask_introduced` | env on check A + introduced on check B → B still INTRODUCED | C10 / FR-009 |
| `test_disabled_gate_identical_to_baseline` | gate off → no env_blocked, behavior unchanged | C11 / SC-006 |
