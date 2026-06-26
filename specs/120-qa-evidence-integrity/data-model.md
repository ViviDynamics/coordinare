# Data Model: Spec 120

No new persisted store and no schema migration. The entities below describe the *in-flight* data the
QA verdict gate and activation path read/produce. Persisted state changes are limited to
backward-compatible defaults on existing models.

## QA Report (existing `PerformerResponse.report: dict | None`)

The performer already emits this dict on QA terminal responses (`main.py` ~L2701). The coordinare gate
(US1) reads these fields; no new transport field is required (`PerformerResponse` has **no**
`extra="ignore"`, and `report` is an open dict).

| Field | Type | Meaning | Used by US1 gate |
|---|---|---|---|
| `criteria_checked` | int | Acceptance criteria the QA run examined | Yes — gate keys on `>0` |
| `criteria_passed` | int | Criteria that actually passed with evidence | Yes — gate keys on `==0` |
| `executed_checks` | list[dict] | `{command, exit_code, output}` proof of runs | Read for evidence presence |
| `evidence_count` | int | executed_checks + new_tests + visual_evidence | Read for evidence presence |
| `environment_error` | str \| None | Honest "couldn't verify" reason | Yes — routes downgrade to HOLD vs bounce |
| `visual_validation_required` | bool | Change affects UI ⇒ screenshot required | Yes — triggers visual-evidence precondition |
| `visual_evidence` | list[dict] | `{label, kind, path_or_url, note}` screenshots | Yes — `path_or_url` presence = captured |
| `app_boot_check` | dict \| None | Proof the app booted (`{command, exit_code}`) | Read by US3 backstop gate |

**Validation rules (US1 gate, coordinare-side):**
- A `qa_passed` status with `criteria_checked > 0 AND criteria_passed == 0` is **unsubstantiated** →
  do not advance.
- A `qa_passed` status with `visual_validation_required == True` AND no `visual_evidence` entry
  having a truthy `path_or_url` is **unsubstantiated** → do not advance.
- An unsubstantiated `qa_passed` with `environment_error` set OR `env_cache_health_failed == True` →
  route to the existing `qa_env_blocked` HOLD path (mark runtime health failed + notify).
- An unsubstantiated `qa_passed` with no environment signal → treat as `qa_failed` (bounce path).
- A `qa_passed` with `criteria_passed >= 1` and required evidence present → **advance unchanged**
  (no regression).

## QA Verdict classification (existing `PerformerResponse.status` literal)

Reuses existing `PerformerStatusType` values — **no new status added**:
`qa_passed` (substantiated success), `qa_env_blocked` (held — environment), `qa_failed` (bounce).

State transition added by US1 (performer-side, `main.py` advisory-pass branch):

```
env_limited AND criteria_passed == 0   →  qa_env_blocked   (was: qa_passed)
env_limited AND criteria_passed >= 1   →  qa_passed        (advisory pass retained)
```

## Environment-health signal (existing)

| Signal | Source | Role |
|---|---|---|
| `report.environment_error` | QA agent JSON → performer report | Marks "couldn't verify"; routes downgrade to HOLD |
| `PerformerResponse.env_cache_health_failed` | `workspace.py` services/health | Existing flag; US2 also sets it when the toolchain fails to resolve |

## Activation result (US2, in-flight, `workspace.py`)

Not persisted. `_activate_env_cache()` returns `cache_env: dict[str,str]`. US2 adds:
- Deterministic `DEVENV=<env_cache_path>` + cleared `_DEVENV_SOURCED` when sourcing `activate.sh`.
- A **toolchain-resolution check**: if the cache advertises a toolchain (e.g. `activate.sh` references
  rbenv) and that toolchain does not resolve on the produced PATH, the activation is treated as failed
  → caller surfaces `environment_error` / sets `env_cache_health_failed`.
- An **observability record** (structlog event, names/reasons only): `env_cache_path` present (bool),
  activation succeeded (bool), toolchain resolved (bool), stage name. No secret values.

## Persisted state (existing snapshot — backward compatible)

No migration. If a per-card marker is needed to avoid duplicate notifications on repeated env-blocked
holds, it is an optional field defaulting to `None`/empty on `PersistedSession`, parallel to the 095
ENV_BLOCKED field — read defensively so old snapshots load unchanged.
