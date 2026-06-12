# Data Model: QA Verdict Integrity & Performer Environment Reliability

## 1. QA Evidence Record (performer-side, extends existing qa output JSON)

The structured object the QA model returns; parsed in `agent/performer/src/performer/main.py`.

| Field | Type | New? | Notes |
|---|---|---|---|
| `criteria_checked` | int | existing | Self-reported count |
| `criteria_passed` | int | existing | Self-reported; now cross-validated (R2) |
| `failures[]` | list | existing | Code-defect findings → `qa_failed` |
| `environment_error` | str | existing | Non-empty ⇒ env-limited run |
| `executed_checks[]` | list[{command, exit_code, summary?}] | existing | Hard evidence channel #1 |
| `new_tests[]` | list | existing | Hard evidence channel #2 |
| `visual_evidence[]` | list[{path_or_url, caption?}] | existing | Hard evidence channel #3 — only *validated* (uploaded) entries count (R5) |
| `visual_capture_blockers[]` | list[str] | existing | Now also receives failed-upload entries (R5) |
| `app_boot_check` | {command, exit_code} \| null | **NEW** | Required when card has UI/visual criteria; must reference an entry in `executed_checks` (R4) |

**Derived values** (computed in `main.py`, not model-reported):
- `evidence_count` = len(executed_checks) + len(new_tests) + len(validated visual_evidence)
- `env_limited` = bool(environment_error) — unchanged
- `unsubstantiated` = claimed pass AND evidence_count == 0 — **no longer bypassed by env_limited**

## 2. QA Verdict (classification produced from the record)

```
                       ┌── failures contain code defects ──────────► qa_failed   (unchanged)
qa output ── parsed ──┼── pass claim + evidence_count ≥ 1
                       │      └─ (+ app_boot ok when visual) ───────► qa_passed   (unchanged)
                       ├── pass claim + evidence_count == 0
                       │      ├─ env_limited ──────────────────────► qa_env_blocked  (NEW terminal)
                       │      └─ not env_limited ──────────────────► malformed/unsubstantiated (existing refusal)
                       └── visual criteria without app_boot ok ─────► those criteria unverified → folds into above
```

**`qa_env_blocked` semantics**: terminal, non-success, non-defect. Coordinare holds the card with structured reason, repairs/re-verifies the env cache, and re-runs the QA stage. Never advances; never triggers the code-fix feedback cycle.

### Status-string propagation sites (closed enums to extend)

| Site | File:line (main @ 45e1b6d) | Change |
|---|---|---|
| Performer state→status mapping | `agent/performer/src/performer/main.py:1576-1585, 2449-2490` | emit `qa_env_blocked` |
| Performer terminal-state tuples | `main.py:2889, 2993` | add `qa_env_blocked` |
| Coordinare terminal markers | `src/coordinare/graph/nodes/monitor_performer.py:35 (TERMINAL_SUCCESS_STATES — NOT added here), :67, :2020` | add to terminal-marker sets as a *non-success* terminal |
| Coordinare verdict branch | `monitor_performer.py:~2031` | new branch: hold + env re-verify |

## 3. EnvCacheState / EnvCacheStateSnapshot (coordinare, persisted)

Existing snapshot (`src/coordinare/state_store.py:162-188`) gains:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `bootstrap_attempts` | int | 0 | Consecutive failed attempts for current `readme_sha` (R7); reset on SHA change and on success |
| `bootstrap_exhausted` | bool | False | Terminal circuit-breaker state; blocks dispatch, names consumer holds (R7) |

Behavioral invariants:
- `on_bootstrap_complete(success=True)` ⇒ attempts=0, exhausted=False, **immediate snapshot flush** (R8 part 1).
- `on_bootstrap_complete(success=False)` ⇒ attempts+=1; attempts ≥ max ⇒ exhausted=True + one notification.
- SHA change ⇒ attempts=0, exhausted=False (existing SHA-rewrite path).
- Restart with `last_bootstrap_succeeded=True ∧ readme_sha==current` ⇒ clean-verify path, never full bootstrap unless verify fails (R8 part 2).
- Old snapshots without the new fields load with defaults (pydantic).

### Bootstrap dispatch state machine (delta)

```
            ┌────────────────────────────────────────────────┐
            ▼                                                │ SHA change (resets budget)
  idle ── needs_bootstrap ── dispatch ── fail ── attempts<N ─┤ (cooldown × 2^attempts)
            │                    │                attempts≥N └► EXHAUSTED ── notify once
            │                    └── success ► READY (attempts=0, snapshot flushed)
            │
   restart: persisted success + SHA match ──► CLEAN-VERIFY ──ok──► READY (no dispatch)
                                                  └─fail──► needs_bootstrap (full)
```

## 4. Performer Session record (coordinare, in-memory)

| Field | Type | New? | Purpose |
|---|---|---|---|
| `secret_refresh_failed_at` | datetime \| None | **NEW** | Set after retry-once fails (R9); monitor attributes later auth failures; session marked degraded |

## 5. Backend Environment Policy (performer, new module)

`agent/performer/src/performer/backends/_env_policy.py`

```
build_subprocess_env(*, cache_env, git_env, tool_env, extra=None) -> dict[str, str]
```

- Layering: `os.environ` → cache (minus PATH) → git → tool → extra (later wins).
- PATH: `image_path = os.environ.get("PATH") or <system default>`; append cache-PATH dirs not already present, in cache order.
- Pure function of inputs + `os.environ`; no I/O; fully unit-testable.
- Consumers: claude_code, openclaw, opencode, opencode_compat, codex, pi, hermes, junie (8 modules; spec says "six backends" — opencode_compat and junie included as conformance targets too).
