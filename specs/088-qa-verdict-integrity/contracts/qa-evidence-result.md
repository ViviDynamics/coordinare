# Contract: QA Evidence Result (performer → coordinare)

The JSON object the QA performer returns (parsed in `agent/performer/src/performer/main.py`)
and the terminal status string surfaced to coordinare's monitor loop. Cross-boundary:
performer image and coordinare daemon must agree on these fields per release.

## Field Registry

| Field | Type | Direction | New in 088? | Meaning |
|---|---|---|---|---|
| `criteria_checked` | int | performer → coordinare (via comment + status) | no | Self-reported count of acceptance criteria examined |
| `criteria_passed` | int | performer → coordinare | no | Self-reported pass count; cross-validated against evidence (FR-007) |
| `failures` | list | performer → coordinare | no | Code-defect findings; non-empty defect set ⇒ `qa_failed` |
| `environment_error` | string | performer → coordinare | no | Non-empty ⇒ env-limited run; no longer exempts evidence check (FR-001) |
| `executed_checks` | list[{command, exit_code, summary?}] | performer → coordinare | no | Evidence channel: commands actually run |
| `new_tests` | list | performer → coordinare | no | Evidence channel: tests committed |
| `visual_evidence` | list[{path_or_url, caption?}] | performer → coordinare | no | Evidence channel: only upload-validated entries render as links (FR-006) |
| `visual_capture_blockers` | list[string] | performer → coordinare | no | Now also receives failed-upload artifacts with reason (FR-006) |
| `app_boot_check` | {command, exit_code} \| null | performer → coordinare | **yes** | Boot-proof for visual/UI criteria; must reference an `executed_checks` entry (FR-005) |
| `qa_env_blocked` | terminal status string | performer → coordinare | **yes** | Pass-claim with zero evidence under an environment blocker; coordinare holds card + repairs env, never advances, never starts fix-feedback (FR-002/003) |
| `env_cache_health_failed` | bool (status payload key) | performer → coordinare | no | Already emitted; NEW consumer behavior: terminal success + this flag ⇒ hold, don't advance (FR-004) |

## Status-enum extension sites (must change together)

- Performer: `main.py:1576-1585, 2449-2490` (emit), `main.py:2889, 2993` (terminal tuples)
- Coordinare: `monitor_performer.py:67, 2020` (terminal markers; NOT in `TERMINAL_SUCCESS_STATES:35`), new verdict branch ~`:2031`

## Compatibility

- Deploy coordinare before the performer image: old performer never emits `qa_env_blocked`; new coordinare handles its absence trivially. New performer + old coordinare would surface an unknown status — avoided by deployment order (documented in quickstart).
- `app_boot_check` is optional/nullable: absent on cards without visual criteria and from old images.
