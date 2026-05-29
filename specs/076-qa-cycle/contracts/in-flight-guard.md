# Contract — In-Flight Dispatch Guard

**Module:** `src/coordinare/services/dispatch_guard.py` + the guard call at the top of `src/coordinare/graph/nodes/dispatch_performer.py`
**FRs:** FR-001, FR-006, FR-007
**SCs:** SC-001, SC-009

## Public API

```python
async def check_inflight(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
) -> InFlightGuardResult: ...

class InFlightGuardResult(BaseModel):
    is_in_flight: bool
    session_id: str | None
    advice: Literal["proceed", "refuse"]
```

A separate module-level `asyncio.Lock` keyed on `(card_id, performer_stage)` MUST be acquired by `dispatch_performer` for the entire duration of its dispatch logic, immediately AFTER `_apply_pending_override` and BEFORE any other side-effect.

## Preconditions

- Called from inside an async context (the graph runs on `asyncio`).
- `state.performer_services` is populated (otherwise no service to query for liveness).

## Postconditions

### Guard refusal path (FR-001)

If `state.agent_dispatch.session_id` is set AND `service.has_live_session(session_id)` returns True:
- Returns `InFlightGuardResult(is_in_flight=True, session_id=…, advice="refuse")`.
- Caller MUST return state unmodified (do NOT advance `phase`, do NOT mutate `agent_dispatch`).
- Emit `dispatch_performer.in_flight_guard_tripped` with `card_id`, `performer_stage`, `session_id`.

### Guard proceed path

If `state.agent_dispatch.session_id` is empty OR the service does not consider it live:
- Returns `InFlightGuardResult(is_in_flight=False, session_id=None, advice="proceed")`.
- Caller proceeds with normal dispatch.

### Mutex semantics (FR-006)

- The lock for `(card_id, performer_stage)` is held for the entire dispatch call.
- Two concurrent `dispatch_performer` invocations for the same `(card_id, performer_stage)` MUST serialise: the second one acquires the lock only after the first releases it, at which point its check_inflight MUST see the result of the first invocation's work.
- The lock dict is module-level on `dispatch_guard.py`; cleanup is a no-op (entries never deleted).

## Required log events

| Event | When | Fields |
|---|---|---|
| `dispatch_performer.in_flight_guard_tripped` | Guard refuses dispatch (FR-001) | `card_id`, `performer_stage`, `session_id`, `service_has_live_session=true` |
| `dispatch_performer.in_flight_guard_passed` | Guard allows dispatch (debug-level OK) | `card_id`, `performer_stage` |
| `dispatch_performer.mutex_waited` | Lock was held by another coroutine, this one waited | `card_id`, `performer_stage`, `wait_ms` |

## Performance budget

- Lock acquisition + check_inflight call: p95 ≤ 5 ms when lock is uncontested.
- Lock acquisition: ≤ 1 ms additional under contention (asyncio.Lock is fair).
- Microbenchmark in `tests/perf/test_reconciliation_latency.py` (same file, separate test).

## Relay-handoff containment (FR-007 / clarification Q5)

Before `dispatch_performer` enters its dispatch logic on a `(card, stage)` that just had its `agent_dispatch` cleared by `monitor_performer`'s `partial_progress` branch (or any other mid-flow clearing path):

1. The clearing path MUST first invoke `dispatch_guard.drain_or_reap(session_id, drain_budget=5.0, reap_budget=5.0)`.
2. `drain_or_reap`:
   - Sends a `POST /jobs/<id>/drain` to the performer's job-runner with a 5 s timeout.
   - If `drain` succeeds within 5 s → returns `("drained", elapsed_ms)`.
   - If `drain` times out → issues `docker stop --time=5 <container_id>`, returns `("reaped", elapsed_ms)`.
   - If `docker stop` fails → escalates to `docker kill`; emits `daemon.reap_failed` if still alive after kill.
3. Total budget hard-capped at 10 s.

## Test coverage

- `tests/unit/services/test_dispatch_guard.py` — `check_inflight` happy/refuse paths, mutex correctness under simulated contention (2 concurrent `asyncio.gather` calls).
- `tests/contract/test_inflight_guard_contract.py` — asserts the 3 log events are emitted with required fields.
- `tests/unit/graph/nodes/test_dispatch_performer_inflight_guard.py` — exercise the integration with `dispatch_performer`.
