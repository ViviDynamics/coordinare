# Contract: EnvCacheState persisted snapshot (state_store)

Durable fields on `EnvCacheStateSnapshot` (`src/coordinare/state_store.py:162-188`).
Cross-restart contract: old snapshots must load into new code (pydantic defaults),
and values written by new code must keep their meaning across restarts.

## Field Registry

| Field | Type | New in 088? | Meaning |
|---|---|---|---|
| `symphony_name` | str | no | Key |
| `sanitised_name` | str | no | Cache dir slug |
| `cache_dir` | str | no | Host cache path |
| `readme_sha` | str \| null | no | Spec-content fingerprint the cache was built for |
| `last_bootstrap_at` | datetime \| null | no | Last attempt timestamp (cooldown base) |
| `last_bootstrap_succeeded` | bool \| null | no | Existing; NEW behavior: flushed to disk immediately on bootstrap completion (FR-011) |
| `last_bootstrap_error` | str \| null | no | Retry feedback + dashboard |
| `cache_dir_ready` | bool | no | Transient-ish readiness mirror |
| `bootstrap_attempts` | int (default 0) | **yes** | Consecutive failures for current `readme_sha`; resets on success or SHA change (FR-009) |
| `bootstrap_exhausted` | bool (default False) | **yes** | Circuit-breaker terminal state; blocks dispatch + names consumer holds; resets on SHA change (FR-009/010) |

## Behavioral contract

- Success ⇒ `attempts=0, exhausted=False`, snapshot flushed before the completion handler returns.
- Failure ⇒ `attempts+=1`; `attempts ≥ env_bootstrap_max_attempts` (config, default 3) ⇒ `exhausted=True` + exactly one notification.
- Restart honor path: `last_bootstrap_succeeded=True ∧ readme_sha==current_sha` ⇒ clean-room verify (`daemon._verify_env_cache_clean`); pass ⇒ ready without bootstrap dispatch (SC-004 < 2 min); fail ⇒ full bootstrap.
- Missing new fields in old snapshots ⇒ defaults (0 / False) — no migration step.
