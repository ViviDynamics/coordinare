# Contract: QA Report fields consumed by the coordinare verdict gate

This is the cross-boundary contract between the **performer** (which emits the QA report) and the
**coordinare** verdict gate (US1, `monitor_performer.py`). No transport change is required — these
fields already ride on `PerformerResponse.report` (an open `dict`; `PerformerResponse` has no
`extra="ignore"`). This file registers the fields the gate depends on so a future payload change
cannot silently drop one.

## Field Registry

| Field | Type | Producer | Consumer (gate) | Required for gate |
|---|---|---|---|---|
| `status` | str (literal) | performer `PerformerResponse.status` | monitor marker | Yes |
| `report.criteria_checked` | int | performer QA report | gate: `>0` test | Yes |
| `report.criteria_passed` | int | performer QA report | gate: `==0` test | Yes |
| `report.environment_error` | str \| None | performer QA report | gate: HOLD-vs-bounce routing | Yes |
| `report.visual_validation_required` | bool | performer QA report | gate: visual-evidence precondition | Yes |
| `report.visual_evidence` | list[dict] | performer QA report | gate: `path_or_url` presence | Yes |
| `report.app_boot_check` | dict \| None | performer QA report | US3 backstop reachability gate | Yes (US3) |
| `env_cache_health_failed` | bool | performer `PerformerResponse` | gate: HOLD routing | Yes |
| `report.executed_checks` | list[dict] | performer QA report | evidence presence | Optional |
| `report.evidence_count` | int | performer QA report | evidence presence | Optional |

## Invariants

- The gate MUST read `report` defensively (`report or {}`), tolerating a missing report (treat as
  unsubstantiated if status is `qa_passed` with no report).
- No field carries secret values; `environment_error` is already truncated to ≤200 chars by the
  performer and contains a reason, not credentials.
- New fields, if any, are additive with backward-compatible defaults; the gate must not require a
  field that an older performer build would omit (degrade to "unsubstantiated" safely, never to a
  false advance).

## Decision table (gate)

| status | criteria_checked | criteria_passed | visual_required | visual_evidence | env signal | Action |
|---|---|---|---|---|---|---|
| qa_passed | >0 | 0 | any | any | yes | **HOLD** (qa_env_blocked) |
| qa_passed | >0 | 0 | any | any | no | **BOUNCE** (qa_failed) |
| qa_passed | >0 | ≥1 | true | none | yes | **HOLD** |
| qa_passed | >0 | ≥1 | true | none | no | **BOUNCE** |
| qa_passed | >0 | ≥1 | true | ≥1 with path_or_url | — | **ADVANCE** |
| qa_passed | >0 | ≥1 | false | — | — | **ADVANCE** |
| qa_passed | 0 | 0 | false | — | — | **ADVANCE** (no criteria scope — unchanged) |
| qa_env_blocked | — | — | — | — | — | **HOLD** (existing path, unchanged) |
| qa_failed | — | — | — | — | — | **BOUNCE** (existing path, unchanged) |

"env signal" = `report.environment_error` truthy OR `env_cache_health_failed == True`.
