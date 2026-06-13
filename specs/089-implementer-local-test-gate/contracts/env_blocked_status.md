# Contract: `env_blocked` terminal status + coordinare route

**Modules**: `agent/performer/src/performer/protocol.py`, `.../main.py`, `src/coordinare/graph/nodes/monitor_performer.py`
**Mirrors**: `qa_env_blocked` (protocol.py lines 36/65; monitor_performer.py lines 2076–2102)

## Performer side

- `protocol.py`: add `"env_blocked"` to the terminal-status literal/sets used for `PerformerResponse.status` and the loop break-conditions.
- `main.py`: add `"env_blocked"` to the terminal-status tuples at the dispatch/poll break points (lines ~3007, ~3111) so the session exits the poll loop on it.
- Implementer done-path: when `_run_test_check` returns `passed=False, env_blocked=True`, set `perf.state = "env_blocked"` and return:

```python
PerformerResponse(
    status="env_blocked",
    session_id=perf.session_id,
    reason=local_result.env_reason,
)
```

No push, no PR, no `changes_requested`.

## Coordinare side (`monitor_performer.py`)

Add `"env_blocked"` to `_terminal_markers` (line ~2060) for slot release, then a route mirroring `qa_env_blocked` (lines 2076–2102), placed before the terminal-success block:

```python
if marker == "env_blocked":
    _reason = str(status.get("reason") or "").strip() or (
        "Performer reported an env-cache blocker during the local test gate"
    )
    logger.warning("monitor_performer.env_blocked", performer_stage=stage,
                   card_id=card_id, reason=_reason)
    _env_cache_svc = state.get("env_cache_service")
    _sym_name = state.get("current_symphony")
    if _env_cache_svc is not None and _sym_name:
        try:
            _env_cache_svc.mark_runtime_health_failed(_sym_name, state)
        except Exception as _exc:
            logger.warning("monitor_performer.env_blocked_mark_failed",
                           card_id=card_id, symphony=_sym_name, error=str(_exc))
    state["env_health_hold_reason"] = f"env_blocked: {_reason}"
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state
```

## Invariants

- `env_blocked` holds the card on the SAME stage (env-cache dispatch gate defers until the cache is repaired). It does NOT advance and does NOT enter the changes_requested fix cycle.
- `local_fix_counter` is NOT incremented on this path (FR-006, SC-003).
- The agent is NOT re-dispatched to fix code.

## Test obligations

- performer returns `status="env_blocked"` with reason when helper reports env_blocked.
- monitor route holds stage, calls `mark_runtime_health_failed`, sets `env_health_hold_reason`, does not advance.
- `local_fix_counter` unchanged after an `env_blocked` outcome.
- slot released on `env_blocked`.
